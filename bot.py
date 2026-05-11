import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY", "")

SYMBOL      = "BTCUSDT"
RISK_PCT    = 0.02
STOP_LOSS   = 0.015
TAKE_PROFIT = 0.03
INTERVAL    = 3600

BASE_URL = "https://api.bybit.com"

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open("log.txt", "a") as f:
            f.write(line + "\n")
    except:
        pass

# ─── BYBIT: FIRMA ─────────────────────────────────────────────────────────────
def get_headers(params=""):
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    sign_str = ts + BYBIT_API_KEY + recv_window + params
    signature = hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json"
    }

# ─── BYBIT: DATI MERCATO ──────────────────────────────────────────────────────
def get_market_data():
    try:
        r = requests.get(
            f"{BASE_URL}/v5/market/kline",
            params={"category": "spot", "symbol": SYMBOL, "interval": "60", "limit": 50},
            timeout=10
        )
        data = r.json()
        candles = data["result"]["list"]
        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = 100 - (100 / (1 + avg_g / avg_l)) if avg_l != 0 else 50

        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26
        macd  = ema12 - ema26

        current   = closes[-1]
        change24h = ((current - closes[0]) / closes[0]) * 100

        return {
            "price":     round(current, 2),
            "change24h": round(change24h, 2),
            "rsi":       round(rsi, 2),
            "macd":      round(macd, 2),
            "volume":    round(sum(volumes[-5:]) / 5, 2),
            "high":      round(max(closes[-24:]), 2),
            "low":       round(min(closes[-24:]), 2),
        }
    except Exception as e:
        log(f"[ERRORE] Dati mercato: {e}")
        return {}

# ─── BYBIT: BILANCIO ─────────────────────────────────────────────────────────
def get_balance():
    try:
        params = "accountType=UNIFIED"
        headers = get_headers(params)
        r = requests.get(
            f"{BASE_URL}/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            headers=headers,
            timeout=10
        )
        data = r.json()
        coins = data["result"]["list"][0]["coin"]
        usdt = next((c for c in coins if c["coin"] == "USDT"), None)
        return float(usdt["availableToWithdraw"]) if usdt else 0.0
    except Exception as e:
        log(f"[ERRORE] Bilancio: {e}")
        return 0.0

# ─── BYBIT: ORDINE ───────────────────────────────────────────────────────────
def place_order(side, price, balance):
    try:
        qty = round((balance * RISK_PCT) / price, 6)
        if qty <= 0:
            log("⚠️ Quantità ordine troppo bassa")
            return False

        body = {
            "category": "spot",
            "symbol": SYMBOL,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": "GoodTillCancel"
        }
        body_str = json.dumps(body)
        headers = get_headers(body_str)
        r = requests.post(
            f"{BASE_URL}/v5/order/create",
            data=body_str,
            headers=headers,
            timeout=10
        )
        result = r.json()
        if result.get("retCode") == 0:
            log(f"✅ Ordine {side} eseguito: {qty} {SYMBOL}")
            return True
        else:
            log(f"❌ Errore ordine: {result.get('retMsg')}")
            return False
    except Exception as e:
        log(f"[ERRORE] Ordine: {e}")
        return False

# ─── NEWS ────────────────────────────────────────────────────────────────────
def get_news():
    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "bitcoin crypto",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 5,
                "apiKey": NEWSAPI_KEY
            },
            timeout=10
        )
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]])
    except Exception as e:
        log(f"[ERRORE] News: {e}")
        return "Nessuna news disponibile"

# ─── FEAR & GREED ────────────────────────────────────────────────────────────
def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        data = r.json()["data"][0]
        return f"{data['value']} ({data['value_classification']})"
    except:
        return "N/A"

# ─── AI DECISION ─────────────────────────────────────────────────────────────
def ai_decision(market, news, fear_greed, balance):
    try:
        prompt = f"""Sei un AI trading agent esperto. Analizza e rispondi SOLO con un JSON.

MERCATO {SYMBOL}:
- Prezzo: ${market.get('price', 0):,.2f}
- Variazione 24h: {market.get('change24h', 0)}%
- RSI: {market.get('rsi', 50)}
- MACD: {market.get('macd', 0)}
- High 24h: ${market.get('high', 0):,.2f}
- Low 24h: ${market.get('low', 0):,.2f}

SENTIMENT:
- Fear & Greed: {fear_greed}
- News:
{news}

PORTAFOGLIO:
- Bilancio USDT: ${balance:.2f}
- Rischio per trade: {RISK_PCT*100}%
- Stop Loss: {STOP_LOSS*100}%
- Take Profit: {TAKE_PROFIT*100}%

Rispondi SOLO con questo JSON (nient'altro):
{{"action": "BUY", "reason": "motivo breve"}}
oppure {{"action": "SELL", "reason": "motivo breve"}}
oppure {{"action": "HOLD", "reason": "motivo breve"}}"""

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        data = r.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return '{"action": "HOLD", "reason": "Errore AI"}'

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    log("🤖 AI Trading Bot avviato!")
    log(f"   Simbolo: {SYMBOL} | Rischio: {RISK_PCT*100}% | SL: {STOP_LOSS*100}% | TP: {TAKE_PROFIT*100}%")

    while True:
        try:
            log("─" * 50)
            log("📊 Raccolta dati...")

            market     = get_market_data()
            balance    = get_balance()
            news       = get_news()
            fear_greed = get_fear_greed()

            if not market:
                log("⚠️ Dati mercato non disponibili, riprovo tra 5 min")
                time.sleep(300)
                continue

            log(f"💰 Prezzo: ${market['price']:,.2f} | RSI: {market['rsi']} | F&G: {fear_greed}")
            log(f"💼 Bilancio: ${balance:.2f} USDT")
            log("🧠 AI in analisi...")

            raw = ai_decision(market, news, fear_greed, balance)
            log(f"🤖 Risposta AI: {raw}")

            try:
                start = raw.find("{")
                end   = raw.rfind("}") + 1
                decision = json.loads(raw[start:end])
                action = decision.get("action", "HOLD")
                reason = decision.get("reason", "")
                log(f"✅ Azione: {action} | Motivo: {reason}")

                if action == "BUY" and balance >= 10:
                    place_order("Buy", market["price"], balance)
                elif action == "SELL" and balance >= 10:
                    place_order("Sell", market["price"], balance)
                else:
                    log("⏸️  HOLD — nessuna operazione")

            except Exception as e:
                log(f"⚠️ Errore parsing: {e}")

            log(f"⏰ Prossima analisi tra {INTERVAL//60} minuti")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("🛑 Bot fermato.")
            break
        except Exception as e:
            log(f"[ERRORE CRITICO] {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
