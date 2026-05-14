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
RISK_PCT  = 0.02
MAX_RISK  = 0.05
SL_PCT    = 0.015
TP_PCT    = 0.03
TRAIL_PCT = 0.0266
INTERVAL  = 3600      # Analisi ogni 60 minuti
MONITOR   = 300       # Monitoraggio posizioni ogni 5 minuti

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

# ─── HYPERLIQUID: POSIZIONI APERTE ───────────────────────────────────────────
def get_positions():
    try:
        account = Account.from_key(HYPERLIQUID_KEY)
        address = account.address
        r = requests.post(f"{HL_URL}/info",
                         json={"type": "clearinghouseState", "user": address},
                         timeout=15)
        data = r.json()
        positions = []
        for pos in data.get("assetPositions", []):
            p = pos.get("position", {})
            size = float(p.get("szi", 0))
            if size != 0:
                positions.append({
                    "coin": p.get("coin"),
                    "size": size,
                    "entry": float(p.get("entryPx", 0)),
                    "pnl": float(p.get("unrealizedPnl", 0)),
                    "side": "LONG" if size > 0 else "SHORT"
                })
        return positions
    except Exception as e:
        log(f"[ERRORE] Posizioni: {e}")
        return []

# ─── HYPERLIQUID: PREZZO CORRENTE ─────────────────────────────────────────────
def get_price():
    try:
        r = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        return float(r.json().get("BTC", 0))
    except:
        return 0.0

# ─── HYPERLIQUID: CHIUDI POSIZIONE ───────────────────────────────────────────
def close_position(size, price, is_long):
    try:
        side = not is_long  # Se long, chiudi con sell; se short, chiudi con buy
        action = {
            "type": "order",
            "orders": [{
                "a": 0,
                "b": side,
                "p": str(round(price * (0.995 if side else 1.005), 2)),
                "s": str(abs(size)),
                "r": True,  # reduce only
                "t": {"limit": {"tif": "Ioc"}},
                "c": ""
            }],
            "grouping": "na"
        }
        payload = hl_sign(action)
        r = requests.post(f"{HL_URL}/exchange", json=payload, timeout=15)
        result = r.json()
        if result.get("status") == "ok":
            log(f"Posizione chiusa con successo!")
            return True
        else:
            log(f"Errore chiusura: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Chiusura: {e}")
        return False

# ─── MONITORAGGIO POSIZIONI ───────────────────────────────────────────────────
def monitor_positions():
    positions = get_positions()
    if not positions:
        return

    price = get_price()
    if price == 0:
        return

    for pos in positions:
        entry  = pos["entry"]
        size   = pos["size"]
        pnl    = pos["pnl"]
        is_long = pos["side"] == "LONG"

        # Calcola SL e TP
        sl = entry * (1 - SL_PCT) if is_long else entry * (1 + SL_PCT)
        tp = entry * (1 + TP_PCT) if is_long else entry * (1 - TP_PCT)
        trail_sl = entry * (1 - TRAIL_PCT) if is_long else entry * (1 + TRAIL_PCT)

        log(f"Posizione {pos['side']} {pos['coin']} | Entry: ${entry:,.2f} | PnL: ${pnl:.2f}")
        log(f"Prezzo: ${price:,.2f} | SL: ${sl:,.2f} | TP: ${tp:,.2f}")

        # Controlla Stop Loss
        if is_long and price <= sl:
            log(f"STOP LOSS scattato! Prezzo ${price:,.2f} <= SL ${sl:,.2f}")
            close_position(size, price, is_long)

        elif not is_long and price >= sl:
            log(f"STOP LOSS scattato! Prezzo ${price:,.2f} >= SL ${sl:,.2f}")
            close_position(size, price, is_long)

        # Controlla Take Profit
        elif is_long and price >= tp:
            log(f"TAKE PROFIT raggiunto! Prezzo ${price:,.2f} >= TP ${tp:,.2f}")
            close_position(size, price, is_long)

        elif not is_long and price <= tp:
            log(f"TAKE PROFIT raggiunto! Prezzo ${price:,.2f} <= TP ${tp:,.2f}")
            close_position(size, price, is_long)

        # Trailing stop — se in profitto del 2%, sposta SL
        elif is_long and price >= entry * 1.02:
            new_sl = price * (1 - TRAIL_PCT)
            if new_sl > sl:
                log(f"Trailing stop aggiornato: ${new_sl:,.2f}")

        elif not is_long and price <= entry * 0.98:
            new_sl = price * (1 + TRAIL_PCT)
            if new_sl < sl:
                log(f"Trailing stop aggiornato: ${new_sl:,.2f}")

        else:
            log(f"Posizione in corso — nessuna azione necessaria")

# ─── DATI MERCATO + INDICATORI ────────────────────────────────────────────────
def get_market_data():
    try:
        r = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        price = float(r.json().get("BTC", 0))

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

        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = round(100 - (100 / (1 + avg_g / avg_l)), 2) if avg_l != 0 else 50

        ema12  = sum(closes[-12:]) / 12
        ema26  = sum(closes[-26:]) / 26
        macd   = round(ema12 - ema26, 2)
        ema20  = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else price
        ema200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else price

        bb_period = 20
        bb_closes = closes[-bb_period:]
        bb_mean   = sum(bb_closes) / bb_period
        bb_std    = math.sqrt(sum((x - bb_mean)**2 for x in bb_closes) / bb_period)
        bb_upper  = round(bb_mean + 2 * bb_std, 2)
        bb_lower  = round(bb_mean - 2 * bb_std, 2)
        bb_mid    = round(bb_mean, 2)

        last_high  = highs[-2]
        last_low   = lows[-2]
        last_close = closes[-2]
        pivot = round((last_high + last_low + last_close) / 3, 2)
        r1    = round(2 * pivot - last_low, 2)
        s1    = round(2 * pivot - last_high, 2)
        r2    = round(pivot + (last_high - last_low), 2)
        s2    = round(pivot - (last_high - last_low), 2)

        trs = []
        for i in range(1, min(15, len(closes))):
            tr = max(highs[-i] - lows[-i],
                    abs(highs[-i] - closes[-i-1]),
                    abs(lows[-i] - closes[-i-1]))
            trs.append(tr)
        atr = round(sum(trs) / len(trs), 2) if trs else 0

        try:
            r3 = requests.post(f"{HL_URL}/info",
                              json={"type": "l2Book", "coin": SYMBOL},
                              timeout=15)
            book = r3.json()
            bids = book.get("levels", [[]])[0][:5]
            asks = book.get("levels", [[]])[1][:5]
            bid_vol = sum(float(b["sz"]) for b in bids)
            ask_vol = sum(float(a["sz"]) for a in asks)
            order_book = f"Bid: {bid_vol:.2f} BTC | Ask: {ask_vol:.2f} BTC | Ratio: {round(bid_vol/ask_vol, 2) if ask_vol > 0 else 'N/A'}"
        except:
            order_book = "N/A"

        change24h = round(((price - closes[-25]) / closes[-25]) * 100, 2) if len(closes) >= 25 else 0
        trend_short = "RIALZISTA" if closes[-1] > closes[-10] else "RIBASSISTA"
        trend_mid   = "RIALZISTA" if closes[-1] > closes[-50] else "RIBASSISTA"
        momentum    = round(((closes[-1] - closes[-10]) / closes[-10]) * 100, 2)
        forecast    = f"Trend 10h: {trend_short} | Trend 50h: {trend_mid} | Momentum: {momentum}%"

        log(f"BTC: ${price:,.2f} | RSI: {rsi} | MACD: {macd} | EMA20: {ema20}")
        log(f"Pivot: {pivot} | R1: {r1} | S1: {s1} | ATR: {atr}")

        return {
            "price": price, "change24h": change24h,
            "rsi": rsi, "macd": macd,
            "ema20": ema20, "ema200": ema200,
            "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_mid": bb_mid,
            "pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2,
            "atr": atr, "order_book": order_book, "forecast": forecast,
            "high": max(closes[-24:]), "low": min(closes[-24:]),
            "volume": round(sum(volumes[-5:]) / 5, 2)
        }
    except Exception as e:
        log(f"[ERRORE] Mercato: {e}")
        return {}

# ─── ALTRI DATI ───────────────────────────────────────────────────────────────
def get_oi_funding():
    try:
        r = requests.post(f"{HL_URL}/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
        data = r.json()
        btc_ctx = data[1][0]
        oi      = float(btc_ctx.get("openInterest", 0))
        funding = float(btc_ctx.get("funding", 0)) * 100
        return f"OI: {oi:,.0f} BTC | Funding: {funding:.4f}%"
    except:
        return "N/A"

def get_news():
    try:
        r = requests.get("https://newsapi.org/v2/everything",
                         params={"q": "bitcoin crypto elon musk trump",
                                 "language": "en", "sortBy": "publishedAt",
                                 "pageSize": 8, "apiKey": NEWSAPI_KEY},
                         timeout=15)
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:8]]) or "Nessuna news"
    except:
        return "Nessuna news"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return f"{d['value']} ({d['value_classification']})"
    except:
        return "N/A"

def get_whale_alert():
    try:
        r = requests.get("https://api.whale-alert.io/v1/transactions",
                        params={"api_key": "free", "min_value": 1000000,
                                "start": int(time.time()) - 3600, "limit": 5},
                        timeout=10)
        txs = r.json().get("transactions", [])
        if not txs:
            return "Nessun movimento balena"
        return "\n".join([f"- {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})" for t in txs[:3]])
    except:
        return "Whale Alert N/A"

# ─── ORDINE ───────────────────────────────────────────────────────────────────
def place_order(side, price, balance):
    try:
        risk   = min(RISK_PCT, MAX_RISK)
        size   = round((balance * risk * LEVERAGE) / price, 4)
        is_buy = side == "BUY"
        sl     = round(price * (1 - SL_PCT) if is_buy else price * (1 + SL_PCT), 2)
        tp     = round(price * (1 + TP_PCT) if is_buy else price * (1 - TP_PCT), 2)

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
            log(f"Ordine {side} eseguito: {size} BTC @ ${price:,.2f}")
            log(f"SL: ${sl:,.2f} | TP: ${tp:,.2f} | Trailing: {TRAIL_PCT*100}%")
            return True
        else:
            log(f"Errore ordine: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Ordine: {e}")
        return False

# ─── AI DECISION ─────────────────────────────────────────────────────────────
def ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions):
    try:
        pos_str = "Nessuna posizione aperta"
        if positions:
            pos_str = "\n".join([f"- {p['side']} {p['coin']}: size={p['size']}, entry=${p['entry']:,.2f}, PnL=${p['pnl']:.2f}" for p in positions])

        prompt = (
            f"Sei un AI trading agent esperto su Hyperliquid. Analizza TUTTI i dati.\n\n"
            f"═══ PREZZO ═══\n"
            f"BTC: ${market.get('price'):,.2f} | 24h: {market.get('change24h')}%\n"
            f"High: ${market.get('high'):,.2f} | Low: ${market.get('low'):,.2f}\n\n"
            f"═══ INDICATORI ═══\n"
            f"RSI: {market.get('rsi')} | MACD: {market.get('macd')}\n"
            f"EMA20: ${market.get('ema20'):,.2f} | EMA200: ${market.get('ema200'):,.2f}\n"
            f"BB Upper: ${market.get('bb_upper'):,.2f} | Lower: ${market.get('bb_lower'):,.2f}\n"
            f"Pivot: {market.get('pivot')} | R1: {market.get('r1')} | S1: {market.get('s1')}\n"
            f"ATR: {market.get('atr')} | {market.get('order_book')}\n\n"
            f"═══ FORECAST ═══\n{market.get('forecast')}\n\n"
            f"═══ DERIVATI ═══\n{oi_funding}\n\n"
            f"═══ SENTIMENT ═══\nFear&Greed: {fear_greed}\n\n"
            f"═══ WHALE ═══\n{whale}\n\n"
            f"═══ NEWS ═══\n{news}\n\n"
            f"═══ PORTAFOGLIO ═══\n"
            f"Bilancio: ${balance:.2f} USDC | Leva: {LEVERAGE}x\n"
            f"Posizioni aperte:\n{pos_str}\n\n"
            f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}% | Trailing: {TRAIL_PCT*100}%\n\n"
            "Rispondi SOLO con uno di questi JSON:\n"
            '{"action":"BUY","reason":"motivo"}\n'
            '{"action":"SELL","reason":"motivo"}\n'
            '{"action":"HOLD","reason":"motivo"}'
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
            return {"action": "HOLD", "reason": "Errore API"}
        text  = resp["content"][0]["text"].strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return {"action": "HOLD", "reason": "Errore AI"}

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    log("AI Trading Bot su Hyperliquid avviato!")
    log(f"Analisi ogni {INTERVAL//60} min | Monitoraggio ogni {MONITOR//60} min")
    log(f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}% | Trailing: {TRAIL_PCT*100}%")

    last_analysis = 0

    while True:
        try:
            now = time.time()

            # ── MONITORAGGIO POSIZIONI (ogni 5 minuti) ──
            log("--- Monitoraggio posizioni ---")
            monitor_positions()

            # ── ANALISI COMPLETA (ogni 60 minuti) ──
            if now - last_analysis >= INTERVAL:
                log("=" * 60)
                log("ANALISI COMPLETA")

                market     = get_market_data()
                balance    = get_balance()
                positions  = get_positions()
                news       = get_news()
                fear_greed = get_fear_greed()
                whale      = get_whale_alert()
                oi_funding = get_oi_funding()

                if not market:
                    log("Dati non disponibili, riprovo tra 5 min")
                else:
                    log(f"Fear&Greed: {fear_greed}")
                    log("AI in analisi...")
                    decision = ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions)
                    action   = decision.get("action", "HOLD")
                    reason   = decision.get("reason", "")
                    log(f"Decisione: {action}")
                    log(f"Motivo: {reason}")

                    if action == "BUY" and balance >= 10 and not positions:
                        place_order("BUY", market["price"], balance)
                    elif action == "SELL" and balance >= 10 and not positions:
                        place_order("SELL", market["price"], balance)
                    else:
                        if positions:
                            log("Posizione già aperta — nessun nuovo ordine")
                        else:
                            log("HOLD - nessuna operazione")

                last_analysis = now

            log(f"Prossimo monitoraggio tra {MONITOR//60} minuti")
            time.sleep(MONITOR)

        except KeyboardInterrupt:
            log("Bot fermato.")
            break
        except Exception as e:
            log(f"[ERRORE CRITICO] {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
