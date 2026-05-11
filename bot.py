import os
import time
import json
import hmac
import hashlib
import requests
from datetime import datetime

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY", "")

SYMBOL   = "BTCUSDT"
RISK_PCT = 0.02
INTERVAL = 3600
BASE_URL = "https://api.bybit.com"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def get_market_data():
    try:
        # Yahoo Finance - nessuna chiave richiesta
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
        params = {"interval": "1h", "range": "2d"}
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]

        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = round(100 - (100 / (1 + avg_g / avg_l)), 2) if avg_l != 0 else 50

        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26
        macd  = round(ema12 - ema26, 2)

        current   = round(closes[-1], 2)
        change24h = round(((current - closes[-25]) / closes[-25]) * 100, 2) if len(closes) >= 25 else 0

        log(f"Prezzo BTC: ${current} | RSI: {rsi} | MACD: {macd}")
        return {"price": current, "change24h": change24h, "rsi": rsi, "macd": macd,
                "high": round(max(closes[-24:]), 2), "low": round(min(closes[-24:]), 2)}
    except Exception as e:
        log(f"[ERRORE] Mercato: {e}")
        return {}

def get_balance():
    try:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        query = "accountType=UNIFIED"
        sign_str = ts + BYBIT_API_KEY + recv_window + query
        sig = hmac.new(BYBIT_API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        r = requests.get(f"{BASE_URL}/v5/account/wallet-balance",
                         params={"accountType": "UNIFIED"}, headers=headers, timeout=15)
        data = r.json()
        if data.get("retCode") == 0:
            coins = data["result"]["list"][0]["coin"]
            usdt = next((c for c in coins if c["coin"] == "USDT"), None)
            bal = float(usdt["availableToWithdraw"]) if usdt else 0.0
            log(f"Bilancio: ${bal} USDT")
            return bal
        else:
            log(f"Bilancio errore: {data.get('retMsg')}")
            return 0.0
    except Exception as e:
        log(f"[ERRORE] Bilancio: {e}")
        return 0.0

def place_order(side, price, balance):
    try:
        qty = round((balance * RISK_PCT) / price, 6)
        body = {"category": "spot", "symbol": SYMBOL, "side": side,
                "orderType": "Market", "qty": str(qty), "timeInForce": "GoodTillCancel"}
        body_str = json.dumps(body, separators=(',', ':'))
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        sign_str = ts + BYBIT_API_KEY + recv_window + body_str
        sig = hmac.new(BYBIT_API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": BYBIT_API_KEY,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }
        r = requests.post(f"{BASE_URL}/v5/order/create", data=body_str, headers=headers, timeout=15)
        result = r.json()
        if result.get("retCode") == 0:
            log(f"Ordine {side} eseguito: {qty} BTC")
        else:
            log(f"Errore ordine: {result.get('retMsg')}")
    except Exception as e:
        log(f"[ERRORE] Ordine: {e}")

def get_news():
    try:
        r = requests.get("https://newsapi.org/v2/everything",
                         params={"q": "bitcoin crypto", "language": "en",
                                 "sortBy": "publishedAt", "pageSize": 5, "apiKey": NEWSAPI_KEY},
                         timeout=15)
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]]) or "Nessuna news"
    except:
        return "Nessuna news"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return f"{d['value']} ({d['value_classification']})"
    except:
        return "N/A"

def ai_decision(market, news, fear_greed, balance):
    try:
        prompt = (
            f"Sei un AI trading agent. Analizza e rispondi SOLO con JSON.\n\n"
            f"BTC Prezzo: ${market.get('price')} | RSI: {market.get('rsi')} | "
            f"MACD: {market.get('macd')} | 24h: {market.get('change24h')}%\n"
            f"Fear&Greed: {fear_greed}\nNews:\n{news}\n"
            f"Bilancio: ${balance} | Rischio: {RISK_PCT*100}%\n\n"
            "Rispondi SOLO con uno di questi:\n"
            '{"action":"BUY","reason":"motivo"}\n'
            '{"action":"SELL","reason":"motivo"}\n'
            '{"action":"HOLD","reason":"motivo"}'
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 150,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        text = r.json()["content"][0]["text"].strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return {"action": "HOLD", "reason": "Errore AI"}

def main():
    log("AI Trading Bot avviato!")
    log(f"Simbolo: {SYMBOL} | Rischio: {RISK_PCT*100}% | Intervallo: {INTERVAL//60}min")

    while True:
        try:
            log("=" * 50)
            market     = get_market_data()
            balance    = get_balance()
            news       = get_news()
            fear_greed = get_fear_greed()

            if not market:
                log("Dati non disponibili, riprovo tra 5 min")
                time.sleep(300)
                continue

            log(f"Fear&Greed: {fear_greed}")
            log("AI in analisi...")
            decision = ai_decision(market, news, fear_greed, balance)
            action = decision.get("action", "HOLD")
            reason = decision.get("reason", "")
            log(f"Decisione: {action} | {reason}")

            if action == "BUY" and balance >= 10:
                place_order("Buy", market["price"], balance)
            elif action == "SELL" and balance >= 10:
                place_order("Sell", market["price"], balance)
            else:
                log("HOLD - nessuna operazione")

            log(f"Prossima analisi tra {INTERVAL//60} minuti")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            log("Bot fermato.")
            break
        except Exception as e:
            log(f"[ERRORE CRITICO] {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
