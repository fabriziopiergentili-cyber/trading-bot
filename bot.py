import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime
from anthropic import Anthropic

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────
BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY")

SYMBOL      = "BTCUSDT"       # Coppia di trading
LEVERAGE    = 2               # Leva massima 2x (sicura)
RISK_PCT    = 0.02            # Rischio massimo 2% per trade
STOP_LOSS   = 0.015           # Stop loss 1.5%
TAKE_PROFIT = 0.03            # Take profit 3%
INTERVAL    = 3600            # Analisi ogni ora (in secondi)

BASE_URL = "https://api.bybit.com"
client   = Anthropic(api_key=ANTHROPIC_KEY)

# ─── BYBIT: FIRMA RICHIESTE ───────────────────────────────────────────────────
def sign_request(params: dict) -> dict:
    ts        = str(int(time.time() * 1000))
    param_str = ts + BYBIT_API_KEY + "5000" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature = hmac.new(BYBIT_API_SECRET.encode(), param_str.encode(), hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": signature, "X-BAPI-RECV-WINDOW": "5000"}

# ─── BYBIT: DATI MERCATO ──────────────────────────────────────────────────────
def get_market_data() -> dict:
    try:
        r = requests.get(f"{BASE_URL}/v5/market/kline",
                         params={"category": "spot", "symbol": SYMBOL, "interval": "60", "limit": 50})
        candles = r.json()["result"]["list"]
        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        # RSI
        gains   = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses  = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g   = sum(gains[-14:])  / 14
        avg_l   = sum(losses[-14:]) / 14
        rsi     = 100 - (100 / (1 + avg_g / avg_l)) if avg_l != 0 else 50

        # MACD
        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26
        macd  = ema12 - ema26

        # Prezzo corrente e variazione
        current   = closes[-1]
        change24h = ((current - closes[0]) / closes[0]) * 100

        return {
            "symbol":    SYMBOL,
            "price":     current,
            "change24h": round(change24h, 2),
            "rsi":       round(rsi, 2),
            "macd":      round(macd, 2),
            "volume":    round(sum(volumes[-5:]) / 5, 2),
            "high":      max(closes[-24:]),
            "low":       min(closes[-24:]),
        }
    except Exception as e:
        print(f"[ERRORE] Dati mercato: {e}")
        return {}

# ─── BYBIT: BILANCIO ─────────────────────────────────────────────────────────
def get_balance() -> float:
    try:
        params  = {"accountType": "UNIFIED"}
        headers = sign_request(params)
        r       = requests.get(f"{BASE_URL}/v5/account/wallet-balance",
                               params=params, headers=headers)
        coins   = r.json()["result"]["list"][0]["coin"]
        usdt    = next((c for c in coins if c["coin"] == "USDT"), None)
        return float(usdt["availableToWithdraw"]) if usdt else 0.0
    except Exception as e:
        print(f"[ERRORE] Bilancio: {e}")
        return 0.0

# ─── BYBIT: PIAZZA ORDINE ────────────────────────────────────────────────────
def place_order(side: str, price: float, balance: float) -> bool:
    try:
        qty     = round((balance * RISK_PCT * LEVERAGE) / price, 6)
        sl      = round(price * (1 - STOP_LOSS) if side == "Buy" else price * (1 + STOP_LOSS), 2)
        tp      = round(price * (1 + TAKE_PROFIT) if side == "Buy" else price * (1 - TAKE_PROFIT), 2)
        params  = {
            "category": "spot", "symbol": SYMBOL, "side": side,
            "orderType": "Market", "qty": str(qty),
            "stopLoss": str(sl), "takeProfit": str(tp),
            "timeInForce": "GoodTillCancel"
        }
        headers = sign_request(params)
        r       = requests.post(f"{BASE_URL}/v5/order/create",
                                json=params, headers=headers)
        result  = r.json()
        if result["retCode"] == 0:
            print(f"[ORDINE ✅] {side} {qty} {SYMBOL} | SL: {sl} | TP: {tp}")
            return True
        else:
            print(f"[ORDINE ❌] {result['retMsg']}")
            return False
    except Exception as e:
        print(f"[ERRORE] Ordine: {e}")
        return False

# ─── NEWS ────────────────────────────────────────────────────────────────────
def get_news() -> str:
    try:
        r = requests.get("https://newsapi.org/v2/everything", params={
            "q": "bitcoin crypto market", "language": "en",
            "sortBy": "publishedAt", "pageSize": 5,
            "apiKey": NEWSAPI_KEY
        })
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]])
    except Exception as e:
        print(f"[ERRORE] News: {e}")
        return "Nessuna news disponibile"

# ─── FEAR & GREED INDEX ──────────────────────────────────────────────────────
def get_fear_greed() -> str:
    try:
        r    = requests.get("https://api.alternative.me/fng/?limit=1")
        data = r.json()["data"][0]
        return f"{data['value']} ({data['value_classification']})"
    except:
        return "N/A"

# ─── AI: DECISIONE ───────────────────────────────────────────────────────────
def ai_decision(market: dict, news: str, fear_greed: str, balance: float) -> str:
    prompt = f"""Sei un AI trading agent esperto in crypto. Analizza i dati e dai UNA sola decisione.

DATI MERCATO:
- Simbolo: {market['symbol']}
- Prezzo: ${market['price']:,.2f}
- Variazione 24h: {market['change24h']}%
- RSI (14): {market['rsi']}
- MACD: {market['macd']}
- Volume medio: {market['volume']}
- High 24h: ${market['high']:,.2f}
- Low 24h: ${market['low']:,.2f}

SENTIMENT:
- Fear & Greed Index: {fear_greed}
- Ultime news:
{news}

PORTAFOGLIO:
- Bilancio USDT disponibile: ${balance:.2f}
- Leva: {LEVERAGE}x
- Rischio per trade: {RISK_PCT*100}%
- Stop Loss: {STOP_LOSS*100}%
- Take Profit: {TAKE_PROFIT*100}%

Rispondi SOLO con uno di questi JSON (niente altro):
{{"action": "BUY", "reason": "spiegazione breve"}}
{{"action": "SELL", "reason": "spiegazione breve"}}
{{"action": "HOLD", "reason": "spiegazione breve"}}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()

# ─── LOG ────────────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open("log.txt", "a") as f:
        f.write(line + "\n")

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    log("🤖 AI Trading Bot avviato!")
    log(f"   Simbolo: {SYMBOL} | Leva: {LEVERAGE}x | Rischio: {RISK_PCT*100}%")

    while True:
        try:
            log("─" * 50)
            log("📊 Raccolta dati mercato...")

            market     = get_market_data()
            balance    = get_balance()
            news       = get_news()
            fear_greed = get_fear_greed()

            log(f"💰 Prezzo: ${market.get('price', 0):,.2f} | RSI: {market.get('rsi', 0)} | F&G: {fear_greed}")
            log(f"💼 Bilancio: ${balance:.2f} USDT")

            log("🧠 AI in analisi...")
            decision_raw = ai_decision(market, news, fear_greed, balance)
            log(f"🤖 Decisione AI: {decision_raw}")

            try:
                decision = json.loads(decision_raw)
                action   = decision.get("action", "HOLD")
                reason   = decision.get("reason", "")
                log(f"✅ Azione: {action} | Motivo: {reason}")

                if action == "BUY" and balance > 10:
                    place_order("Buy", market["price"], balance)
                elif action == "SELL" and balance > 10:
                    place_order("Sell", market["price"], balance)
                else:
                    log("⏸️  HOLD — nessuna operazione")

            except json.JSONDecodeError:
                log(f"⚠️  Risposta AI non valida: {decision_raw}")

            log(f"⏰ Prossima analisi tra {INTERVAL//60} minuti...")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("🛑 Bot fermato manualmente.")
            break
        except Exception as e:
            log(f"[ERRORE CRITICO] {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
