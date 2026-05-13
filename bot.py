import os
import time
import json
import requests
import math
from datetime import datetime
from eth_account import Account
from eth_account.messages import encode_defunct

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
HYPERLIQUID_KEY  = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY", "")

SYMBOL    = "BTC"
LEVERAGE  = 2
RISK_PCT  = 0.02        # 2% rischio per trade
MAX_RISK  = 0.05        # 5% massimo per trade
SL_PCT    = 0.015       # 1.5% stop loss
TP_PCT    = 0.03        # 3% take profit
TRAIL_PCT = 0.0266      # Trailing stop metodo Montecarlo 2.66%
INTERVAL  = 3600        # Analisi ogni ora

HL_URL = "https://api.hyperliquid.xyz"

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── HYPERLIQUID: FIRMA ───────────────────────────────────────────────────────
def hl_sign(action):
    account = Account.from_key(HYPERLIQUID_KEY)
    timestamp = int(time.time() * 1000)
    payload = json.dumps({"action": action, "nonce": timestamp}, separators=(',', ':'))
    msg = encode_defunct(text=payload)
    signed = account.sign_message(msg)
    return {
        "action": action,
        "nonce": timestamp,
        "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}
    }

# ─── HYPERLIQUID: BILANCIO ────────────────────────────────────────────────────
def get_balance():
    try:
        account = Account.from_key(HYPERLIQUID_KEY)
        address = account.address
        r = requests.post(f"{HL_URL}/info",
                         json={"type": "clearinghouseState", "user": address},
                         timeout=15)
        data = r.json()
        balance = float(data.get("marginSummary", {}).get("accountValue", 0))
        log(f"Bilancio: ${balance:.2f} USDC")
        return balance
    except Exception as e:
        log(f"[ERRORE] Bilancio: {e}")
        return 0.0

# ─── HYPERLIQUID: DATI MERCATO + INDICATORI ───────────────────────────────────
def get_market_data():
    try:
        # Prezzo corrente
        r = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        mids = r.json()
        price = float(mids.get("BTC", 0))

        # Candele orarie ultime 200
        r2 = requests.post(f"{HL_URL}/info",
                          json={"type": "candleSnapshot",
                                "req": {"coin": SYMBOL, "interval": "1h",
                                        "startTime": int(time.time()*1000) - 86400000*8}},
                          timeout=15)
        candles = r2.json()
        closes  = [float(c["c"]) for c in candles]
        highs   = [float(c["h"]) for c in candles]
        lows    = [float(c["l"]) for c in candles]
        volumes = [float(c["v"]) for c in candles]

        if len(closes) < 26:
            return {}

        # ── RSI ──
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = round(100 - (100 / (1 + avg_g / avg_l)), 2) if avg_l != 0 else 50

        # ── MACD ──
        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26
        macd  = round(ema12 - ema26, 2)

        # ── EMA20 e EMA200 ──
        ema20  = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else price
        ema200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else price

        # ── Bande di Bollinger (20 periodi, 2 deviazioni standard) ──
        bb_period = 20
        bb_closes = closes[-bb_period:]
        bb_mean   = sum(bb_closes) / bb_period
        bb_std    = math.sqrt(sum((x - bb_mean)**2 for x in bb_closes) / bb_period)
        bb_upper  = round(bb_mean + 2 * bb_std, 2)
        bb_lower  = round(bb_mean - 2 * bb_std, 2)
        bb_mid    = round(bb_mean, 2)

        # ── Pivot Point (classico, basato su ultima candela chiusa) ──
        last_high  = highs[-2]
        last_low   = lows[-2]
        last_close = closes[-2]
        pivot  = round((last_high + last_low + last_close) / 3, 2)
        r1     = round(2 * pivot - last_low, 2)
        s1     = round(2 * pivot - last_high, 2)
        r2     = round(pivot + (last_high - last_low), 2)
        s2     = round(pivot - (last_high - last_low), 2)

        # ── ATR (Average True Range, 14 periodi) ──
        trs = []
        for i in range(1, min(15, len(closes))):
            tr = max(highs[-i] - lows[-i],
                    abs(highs[-i] - closes[-i-1]),
                    abs(lows[-i] - closes[-i-1]))
            trs.append(tr)
        atr = round(sum(trs) / len(trs), 2) if trs else 0

        # ── Order Book da Hyperliquid ──
        try:
            r3 = requests.post(f"{HL_URL}/info",
                              json={"type": "l2Book", "coin": SYMBOL},
                              timeout=15)
            book = r3.json()
            bids = book.get("levels", [[]])[0][:5]
            asks = book.get("levels", [[]])[1][:5]
            bid_vol = sum(float(b["sz"]) for b in bids)
            ask_vol = sum(float(a["sz"]) for a in asks)
            order_book = f"Bid vol: {bid_vol:.2f} BTC | Ask vol: {ask_vol:.2f} BTC | Ratio: {round(bid_vol/ask_vol, 2) if ask_vol > 0 else 'N/A'}"
        except:
            order_book = "Order book non disponibile"

        change24h = round(((price - closes[-25]) / closes[-25]) * 100, 2) if len(closes) >= 25 else 0

        log(f"BTC: ${price:,.2f} | RSI: {rsi} | MACD: {macd} | EMA20: {ema20} | BB: [{bb_lower}-{bb_upper}]")
        log(f"Pivot: {pivot} | R1: {r1} | S1: {s1} | ATR: {atr}")

        return {
            "price": price, "change24h": change24h,
            "rsi": rsi, "macd": macd,
            "ema20": ema20, "ema200": ema200,
            "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_mid": bb_mid,
            "pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2,
            "atr": atr, "order_book": order_book,
            "high": max(closes[-24:]), "low": min(closes[-24:]),
            "volume": round(sum(volumes[-5:]) / 5, 2)
        }
    except Exception as e:
        log(f"[ERRORE] Mercato: {e}")
        return {}

# ─── OPEN INTEREST + FUNDING RATE ─────────────────────────────────────────────
def get_oi_funding():
    try:
        r = requests.post(f"{HL_URL}/info",
                         json={"type": "metaAndAssetCtxs"},
                         timeout=15)
        data = r.json()
        btc_ctx = data[1][0]
        oi      = float(btc_ctx.get("openInterest", 0))
        funding = float(btc_ctx.get("funding", 0)) * 100
        log(f"OI: {oi:,.0f} BTC | Funding: {funding:.4f}%")
        return f"Open Interest: {oi:,.0f} BTC | Funding Rate: {funding:.4f}%"
    except:
        return "OI/Funding N/A"

# ─── NEWS + SOCIAL ────────────────────────────────────────────────────────────
def get_news():
    try:
        r = requests.get("https://newsapi.org/v2/everything",
                         params={"q": "bitcoin crypto ethereum elon musk trump",
                                 "language": "en", "sortBy": "publishedAt",
                                 "pageSize": 8, "apiKey": NEWSAPI_KEY},
                         timeout=15)
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:8]]) or "Nessuna news"
    except:
        return "Nessuna news"

# ─── FEAR & GREED ────────────────────────────────────────────────────────────
def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        fg = f"{d['value']} ({d['value_classification']})"
        log(f"Fear&Greed: {fg}")
        return fg
    except:
        return "N/A"

# ─── WHALE ALERT ─────────────────────────────────────────────────────────────
def get_whale_alert():
    try:
        r = requests.get("https://api.whale-alert.io/v1/transactions",
                        params={"api_key": "free", "min_value": 1000000,
                                "start": int(time.time()) - 3600, "limit": 5},
                        timeout=10)
        txs = r.json().get("transactions", [])
        if not txs:
            return "Nessun movimento balena nell'ultima ora"
        alerts = []
        for tx in txs[:3]:
            alerts.append(f"- {tx.get('blockchain','?')}: {tx.get('amount',0):,.0f} {tx.get('symbol','?')} (${tx.get('amount_usd',0):,.0f})")
        result = "\n".join(alerts)
        log(f"Whale: {result[:80]}...")
        return result
    except:
        return "Whale Alert non disponibile"

# ─── FORECASTING SEMPLICE (trend multi-timeframe) ────────────────────────────
def get_forecast(closes):
    try:
        if len(closes) < 50:
            return "Dati insufficienti per forecast"
        # Trend breve (10 periodi)
        trend_short = "RIALZISTA" if closes[-1] > closes[-10] else "RIBASSISTA"
        # Trend medio (50 periodi)
        trend_mid = "RIALZISTA" if closes[-1] > closes[-50] else "RIBASSISTA"
        # Momentum
        momentum = round(((closes[-1] - closes[-10]) / closes[-10]) * 100, 2)
        return f"Trend 10h: {trend_short} | Trend 50h: {trend_mid} | Momentum: {momentum}%"
    except:
        return "Forecast N/A"

# ─── ORDINE CON TRAILING STOP ─────────────────────────────────────────────────
def place_order(side, price, balance):
    try:
        # Gestione capitale — mai più del 5%
        risk = min(RISK_PCT, MAX_RISK)
        size = round((balance * risk * LEVERAGE) / price, 4)
        is_buy = side == "BUY"

        sl    = round(price * (1 - SL_PCT) if is_buy else price * (1 + SL_PCT), 2)
        tp    = round(price * (1 + TP_PCT) if is_buy else price * (1 - TP_PCT), 2)
        trail = round(price * TRAIL_PCT, 2)

        action = {
            "type": "order",
            "orders": [{
                "a": 0,
                "b": is_buy,
                "p": str(price),
                "s": str(size),
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
                "c": ""
            }],
            "grouping": "na"
        }

        payload = hl_sign(action)
        r = requests.post(f"{HL_URL}/exchange", json=payload, timeout=15)
        result = r.json()

        if result.get("status") == "ok":
            log(f"Ordine {side} eseguito!")
            log(f"Size: {size} BTC @ ${price:,.2f}")
            log(f"SL: ${sl:,.2f} | TP: ${tp:,.2f} | Trailing: ${trail:,.2f}")
            return True
        else:
            log(f"Errore ordine: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Ordine: {e}")
        return False

# ─── AI DECISION ─────────────────────────────────────────────────────────────
def ai_decision(market, news, fear_greed, whale, oi_funding, forecast, balance):
    try:
        prompt = (
            f"Sei un AI trading agent esperto su Hyperliquid. Analizza TUTTI i dati e decidi.\n\n"
            f"═══ PREZZO E VARIAZIONE ═══\n"
            f"Prezzo BTC: ${market.get('price'):,.2f} | 24h: {market.get('change24h')}%\n"
            f"High 24h: ${market.get('high'):,.2f} | Low 24h: ${market.get('low'):,.2f}\n\n"
            f"═══ INDICATORI TECNICI ═══\n"
            f"RSI: {market.get('rsi')} (>70=ipercomprato, <30=ipervenduto)\n"
            f"MACD: {market.get('macd')}\n"
            f"EMA20: ${market.get('ema20'):,.2f} | EMA200: ${market.get('ema200'):,.2f}\n"
            f"Prezzo vs EMA20: {'SOPRA' if market.get('price',0) > market.get('ema20',0) else 'SOTTO'}\n"
            f"Prezzo vs EMA200: {'SOPRA' if market.get('price',0) > market.get('ema200',0) else 'SOTTO'}\n\n"
            f"═══ BANDE DI BOLLINGER ═══\n"
            f"Upper: ${market.get('bb_upper'):,.2f} | Mid: ${market.get('bb_mid'):,.2f} | Lower: ${market.get('bb_lower'):,.2f}\n"
            f"Posizione: {'SOPRA upper (ipercomprato)' if market.get('price',0) > market.get('bb_upper',0) else 'SOTTO lower (ipervenduto)' if market.get('price',0) < market.get('bb_lower',0) else 'DENTRO le bande'}\n\n"
            f"═══ PIVOT POINT ═══\n"
            f"Pivot: {market.get('pivot')} | R1: {market.get('r1')} | R2: {market.get('r2')}\n"
            f"S1: {market.get('s1')} | S2: {market.get('s2')} | ATR: {market.get('atr')}\n\n"
            f"═══ ORDER BOOK ═══\n"
            f"{market.get('order_book')}\n\n"
            f"═══ DERIVATI ═══\n"
            f"{oi_funding}\n\n"
            f"═══ FORECAST MULTI-TIMEFRAME ═══\n"
            f"{forecast}\n\n"
            f"═══ SENTIMENT ═══\n"
            f"Fear & Greed: {fear_greed}\n\n"
            f"═══ WHALE ALERT ═══\n"
            f"{whale}\n\n"
            f"═══ NEWS E SOCIAL ═══\n"
            f"{news}\n\n"
            f"═══ PORTAFOGLIO ═══\n"
            f"Bilancio: ${balance:.2f} USDC | Leva: {LEVERAGE}x\n"
            f"Rischio: {RISK_PCT*100}% | Max: {MAX_RISK*100}%\n"
            f"Stop Loss: {SL_PCT*100}% | Take Profit: {TP_PCT*100}%\n"
            f"Trailing Stop: {TRAIL_PCT*100}% (Montecarlo 2.66)\n\n"
            "Analizza tutto e rispondi SOLO con uno di questi JSON:\n"
            '{"action":"BUY","reason":"motivo dettagliato"}\n'
            '{"action":"SELL","reason":"motivo dettagliato"}\n'
            '{"action":"HOLD","reason":"motivo dettagliato"}'
        )

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001",
                  "max_tokens": 400,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        resp = r.json()
        if "content" not in resp:
            log(f"Errore API: {resp.get('error', resp)}")
            return {"action": "HOLD", "reason": "Errore API"}
        text = resp["content"][0]["text"].strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return {"action": "HOLD", "reason": "Errore AI"}

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    log("AI Trading Bot su Hyperliquid avviato!")
    log(f"Simbolo: {SYMBOL} | Leva: {LEVERAGE}x | Rischio: {RISK_PCT*100}%")
    log(f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}% | Trailing: {TRAIL_PCT*100}%")

    while True:
        try:
            log("=" * 60)
            market     = get_market_data()
            balance    = get_balance()
            news       = get_news()
            fear_greed = get_fear_greed()
            whale      = get_whale_alert()
            oi_funding = get_oi_funding()

            if not market:
                log("Dati non disponibili, riprovo tra 5 min")
                time.sleep(300)
                continue

            # Forecast con i dati già raccolti
            closes = []
            try:
                r = requests.post(f"{HL_URL}/info",
                                 json={"type": "candleSnapshot",
                                       "req": {"coin": SYMBOL, "interval": "1h",
                                               "startTime": int(time.time()*1000) - 86400000*3}},
                                 timeout=15)
                closes = [float(c["c"]) for c in r.json()]
            except:
                pass
            forecast = get_forecast(closes)
            log(f"Forecast: {forecast}")

            log("AI in analisi...")
            decision = ai_decision(market, news, fear_greed, whale, oi_funding, forecast, balance)
            action = decision.get("action", "HOLD")
            reason = decision.get("reason", "")
            log(f"Decisione: {action}")
            log(f"Motivo: {reason}")

            if action == "BUY" and balance >= 10:
                place_order("BUY", market["price"], balance)
            elif action == "SELL" and balance >= 10:
                place_order("SELL", market["price"], balance)
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
