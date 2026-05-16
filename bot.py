import os
import time
import json
import requests
import math
from datetime import datetime
from collections import deque
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
HYPERLIQUID_KEY  = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
HYPERLIQUID_ADDR = os.environ.get("HYPERLIQUID_ADDRESS", "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL    = "BTC"
LEVERAGE  = 2
RISK_PCT  = 0.02
MAX_RISK  = 0.05
SL_PCT    = 0.015
TP_PCT    = 0.03
TRAIL_PCT = 0.0266
INTERVAL  = 3600
MONITOR   = 300

# History settings
MAX_HISTORY = 20  # Max number of analysis cycles to remember

HL_URL = "https://api.hyperliquid.xyz"

# ─── SETUP SDK ────────────────────────────────────────────────────────────────
account        = Account.from_key(HYPERLIQUID_KEY)
hl_info        = Info(constants.MAINNET_API_URL)
exchange_open  = Exchange(account, constants.MAINNET_API_URL)
exchange_close = Exchange(account, constants.MAINNET_API_URL, account_address=HYPERLIQUID_ADDR)

# ─── DECISION HISTORY ─────────────────────────────────────────────────────────
# Each entry: {"ts": "...", "price": ..., "rsi": ..., "action": "...", "reason": "...", "outcome": "..."}
decision_history = deque(maxlen=MAX_HISTORY)

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def notify(msg, important=False):
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        prefix = "🚨 " if important else "🤖 "
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"{prefix}*AI Trading Bot*\n\n{msg}",
                  "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"[ERRORE] Telegram: {e}")

# ─── BILANCIO SPOT ────────────────────────────────────────────────────────────
def get_balance():
    try:
        r        = requests.post(f"{HL_URL}/info",
                                 json={"type": "spotClearinghouseState", "user": HYPERLIQUID_ADDR},
                                 timeout=15)
        balances = r.json().get("balances", [])
        usdc     = next((b for b in balances if b["coin"] == "USDC"), None)
        balance  = float(usdc["total"]) if usdc else 0.0
        log(f"Bilancio USDC: ${balance:.2f}")
        return balance
    except Exception as e:
        log(f"[ERRORE] Bilancio: {e}")
        return 0.0

# ─── POSIZIONI APERTE ─────────────────────────────────────────────────────────
def get_positions():
    try:
        state     = hl_info.user_state(HYPERLIQUID_ADDR)
        positions = []
        for pos in state.get("assetPositions", []):
            p    = pos.get("position", {})
            size = float(p.get("szi", 0))
            if size != 0:
                positions.append({
                    "coin":  p.get("coin"),
                    "size":  size,
                    "entry": float(p.get("entryPx", 0)),
                    "pnl":   float(p.get("unrealizedPnl", 0)),
                    "side":  "LONG" if size > 0 else "SHORT"
                })
        return positions
    except Exception as e:
        log(f"[ERRORE] Posizioni: {e}")
        return []

# ─── PREZZO CORRENTE ──────────────────────────────────────────────────────────
def get_price():
    try:
        r = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        return float(r.json().get("BTC", 0))
    except:
        return 0.0

# ─── APRI ORDINE ─────────────────────────────────────────────────────────────
def place_order(side, price, balance):
    try:
        size     = round((balance * min(RISK_PCT, MAX_RISK) * LEVERAGE) / price, 5)
        min_size = round(10 / price * 1.01, 5)
        size     = max(size, min_size)
        is_buy   = side == "BUY"
        sl       = round(price * (1 - SL_PCT) if is_buy else price * (1 + SL_PCT), 2)
        tp       = round(price * (1 + TP_PCT)  if is_buy else price * (1 - TP_PCT), 2)

        log(f"Apertura {side}: {size} BTC @ ${price:,.2f}")
        result = exchange_open.market_open(SYMBOL, is_buy, size)

        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" not in statuses[0]:
                log(f"Ordine {side} eseguito! SL: ${sl:,.2f} | TP: ${tp:,.2f}")
                notify(
                    f"{'🟢' if is_buy else '🔴'} *Ordine {side} aperto!*\n"
                    f"BTC @ ${price:,.2f}\n"
                    f"Size: {size} BTC\n"
                    f"Stop Loss: ${sl:,.2f}\n"
                    f"Take Profit: ${tp:,.2f}",
                    important=True
                )
                return True
            else:
                log(f"Errore ordine: {statuses}")
                return False
        else:
            log(f"Errore ordine: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Ordine: {e}")
        return False

# ─── CHIUDI POSIZIONE ─────────────────────────────────────────────────────────
def close_position(pos):
    try:
        coin   = pos["coin"]
        size   = abs(pos["size"])
        log(f"Chiusura posizione {pos['side']} {coin} size={size}")
        result = exchange_close.market_close(coin, sz=size)
        if result and result.get("status") == "ok":
            log(f"Posizione {coin} chiusa!")
            return True
        else:
            log(f"Errore chiusura: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Chiusura: {e}")
        return False

# ─── MONITORAGGIO POSIZIONI ───────────────────────────────────────────────────
def monitor_positions():
    positions       = get_positions()
    position_closed = False

    if not positions:
        return False

    price = get_price()

    for pos in positions:
        entry   = pos["entry"]
        pnl     = pos["pnl"]
        is_long = pos["side"] == "LONG"
        sl      = entry * (1 - SL_PCT) if is_long else entry * (1 + SL_PCT)
        tp      = entry * (1 + TP_PCT) if is_long else entry * (1 - TP_PCT)

        log(f"Posizione {pos['side']} {pos['coin']} | Entry: ${entry:,.2f} | PnL: ${pnl:.2f} | Prezzo: ${price:,.2f}")

        if (is_long and price <= sl) or (not is_long and price >= sl):
            log("STOP LOSS scattato!")
            if close_position(pos):
                position_closed = True
                # Update last history entry with outcome
                if decision_history:
                    decision_history[-1]["outcome"] = f"STOP LOSS @ ${price:,.2f} | PnL: ${pnl:.2f}"
                notify(
                    f"🔴 *STOP LOSS scattato!*\n"
                    f"{pos['side']} BTC\n"
                    f"Entry: ${entry:,.2f} → ${price:,.2f}\n"
                    f"PnL: ${pnl:.2f} USDC",
                    important=True
                )

        elif (is_long and price >= tp) or (not is_long and price <= tp):
            log("TAKE PROFIT raggiunto!")
            if close_position(pos):
                position_closed = True
                if decision_history:
                    decision_history[-1]["outcome"] = f"TAKE PROFIT @ ${price:,.2f} | PnL: ${pnl:.2f}"
                notify(
                    f"✅ *TAKE PROFIT raggiunto!*\n"
                    f"{pos['side']} BTC\n"
                    f"Entry: ${entry:,.2f} → ${price:,.2f}\n"
                    f"PnL: ${pnl:.2f} USDC",
                    important=True
                )

        elif is_long and price >= entry * 1.02:
            new_sl = price * (1 - TRAIL_PCT)
            if new_sl > sl:
                log(f"Trailing stop aggiornato: ${new_sl:,.2f}")
                notify(f"📈 Trailing stop aggiornato: ${new_sl:,.2f}")
        elif not is_long and price <= entry * 0.98:
            new_sl = price * (1 + TRAIL_PCT)
            if new_sl < sl:
                log(f"Trailing stop aggiornato: ${new_sl:,.2f}")
                notify(f"📉 Trailing stop aggiornato: ${new_sl:,.2f}")
        else:
            log("Posizione in corso — nessuna azione")

    return position_closed

# ─── DATI MERCATO ─────────────────────────────────────────────────────────────
def get_market_data():
    try:
        r     = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        price = float(r.json().get("BTC", 0))

        r2      = requests.post(f"{HL_URL}/info",
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
        ema20  = round(sum(closes[-20:]) / 20, 2)
        ema200 = round(sum(closes[-200:]) / 200, 2) if len(closes) >= 200 else round(sum(closes) / len(closes), 2)

        bb_closes = closes[-20:]
        bb_mean   = sum(bb_closes) / 20
        bb_std    = math.sqrt(sum((x - bb_mean)**2 for x in bb_closes) / 20)
        bb_upper  = round(bb_mean + 2 * bb_std, 2)
        bb_lower  = round(bb_mean - 2 * bb_std, 2)

        last_high  = highs[-2]
        last_low   = lows[-2]
        last_close = closes[-2]
        pivot = round((last_high + last_low + last_close) / 3, 2)
        r1    = round(2 * pivot - last_low, 2)
        s1    = round(2 * pivot - last_high, 2)
        r2    = round(pivot + (last_high - last_low), 2)
        s2    = round(pivot - (last_high - last_low), 2)

        trs = [max(highs[-i]-lows[-i], abs(highs[-i]-closes[-i-1]), abs(lows[-i]-closes[-i-1]))
               for i in range(1, min(15, len(closes)))]
        atr = round(sum(trs) / len(trs), 2) if trs else 0

        try:
            r3      = requests.post(f"{HL_URL}/info", json={"type": "l2Book", "coin": SYMBOL}, timeout=15)
            book    = r3.json()
            bids    = book.get("levels", [[]])[0][:5]
            asks    = book.get("levels", [[]])[1][:5]
            bid_vol = sum(float(b["sz"]) for b in bids)
            ask_vol = sum(float(a["sz"]) for a in asks)
            order_book = f"Bid: {bid_vol:.2f} | Ask: {ask_vol:.2f} | Ratio: {round(bid_vol/ask_vol,2) if ask_vol>0 else 'N/A'}"
        except:
            order_book = "N/A"

        change24h   = round(((price - closes[-25]) / closes[-25]) * 100, 2) if len(closes) >= 25 else 0
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
            "bb_upper": bb_upper, "bb_lower": bb_lower,
            "pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2,
            "atr": atr, "order_book": order_book, "forecast": forecast,
            "high": max(closes[-24:]), "low": min(closes[-24:]),
            "volume": round(sum(volumes[-5:]) / 5, 2)
        }
    except Exception as e:
        log(f"[ERRORE] Mercato: {e}")
        return {}

def get_oi_funding():
    try:
        r       = requests.post(f"{HL_URL}/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
        btc_ctx = r.json()[1][0]
        oi      = float(btc_ctx.get("openInterest", 0))
        funding = float(btc_ctx.get("funding", 0)) * 100
        return f"OI: {oi:,.0f} BTC | Funding: {funding:.4f}%"
    except:
        return "N/A"

def get_news():
    try:
        r        = requests.get("https://newsapi.org/v2/everything",
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
        r   = requests.get("https://api.whale-alert.io/v1/transactions",
                           params={"api_key": "free", "min_value": 1000000,
                                   "start": int(time.time()) - 3600, "limit": 5},
                           timeout=10)
        txs = r.json().get("transactions", [])
        if not txs:
            return "Nessun movimento balena"
        return "\n".join([f"- {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})"
                          for t in txs[:3]])
    except:
        return "Whale Alert N/A"

# ─── BUILD HISTORY STRING ─────────────────────────────────────────────────────
def build_history_string():
    """Builds a summary of past decisions to include in the prompt."""
    if not decision_history:
        return "No decision history yet — this is the first analysis."

    lines = ["DECISION HISTORY (oldest to newest):"]
    for i, h in enumerate(decision_history):
        outcome = h.get("outcome", "still open / not yet resolved")
        lines.append(
            f"[{i+1}] {h['ts']} | BTC: ${h['price']:,.0f} | RSI: {h['rsi']} | "
            f"Action: {h['action']} | Outcome: {outcome}"
        )

    # Add a summary of hold streak
    recent = list(decision_history)[-5:]
    hold_streak = sum(1 for h in recent if h["action"] == "HOLD")
    if hold_streak >= 3:
        lines.append(f"\n⚠️ WARNING: You have been HOLD for {hold_streak} consecutive cycles.")
        lines.append("Consider whether market conditions have changed enough to act.")

    return "\n".join(lines)

# ─── AI DECISION ─────────────────────────────────────────────────────────────
def ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions):
    try:
        pos_str = "No open positions"
        if positions:
            pos_str = "\n".join([
                f"- {p['side']} {p['coin']}: size={p['size']}, entry=${p['entry']:,.2f}, PnL=${p['pnl']:.2f}"
                for p in positions
            ])

        history_str = build_history_string()

        # ── SYSTEM PROMPT ──
        system_prompt = """You are an expert AI trading agent on Hyperliquid. 
Your goal is to maximize returns while managing risk carefully.

CRITICAL RULES:
1. You have a decision history. Learn from it — if you keep saying HOLD without acting, you are missing opportunities.
2. If you have been HOLD for 3+ consecutive cycles, you MUST either find a reason to act or explicitly explain why the market is truly untradeable.
3. When RSI < 35 and trend shows any sign of recovery → strongly consider BUY.
4. When RSI > 65 and trend weakens → strongly consider SELL.
5. Do NOT be overly cautious. A trading bot that never trades is useless.
6. Learn from past outcomes: if a previous BUY hit stop loss, be more careful. If it hit take profit, similar setups are valid.
7. Always respond ONLY with valid JSON: {"action":"BUY","reason":"..."} or {"action":"SELL","reason":"..."} or {"action":"HOLD","reason":"..."}"""

        # ── CURRENT MARKET MESSAGE ──
        current_market = (
            f"=== CURRENT MARKET DATA ===\n"
            f"BTC: ${market.get('price'):,.2f} | 24h: {market.get('change24h')}%\n"
            f"RSI: {market.get('rsi')} | MACD: {market.get('macd')}\n"
            f"EMA20: ${market.get('ema20'):,.2f} | EMA200: ${market.get('ema200'):,.2f}\n"
            f"BB Upper: ${market.get('bb_upper'):,.2f} | Lower: ${market.get('bb_lower'):,.2f}\n"
            f"Pivot: {market.get('pivot')} | R1: {market.get('r1')} | S1: {market.get('s1')}\n"
            f"ATR: {market.get('atr')} | {market.get('order_book')}\n"
            f"Forecast: {market.get('forecast')}\n"
            f"{oi_funding}\n"
            f"Fear&Greed: {fear_greed}\n"
            f"Whale: {whale}\n"
            f"News:\n{news}\n\n"
            f"=== PORTFOLIO ===\n"
            f"Balance: ${balance:.2f} USDC | Leverage: {LEVERAGE}x\n"
            f"Open positions:\n{pos_str}\n"
            f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}% | Trailing: {TRAIL_PCT*100}%\n\n"
            f"=== {history_str} ===\n\n"
            f"Based on ALL the above, what is your trading decision? "
            f"Remember: if you keep HOLDing, justify it clearly or find an opportunity to act."
        )

        # ── BUILD MESSAGES WITH HISTORY AS CHAT ──
        messages = []

        # Add past decisions as alternating user/assistant messages
        for h in list(decision_history)[:-1]:  # exclude last (current)
            messages.append({
                "role": "user",
                "content": (
                    f"Analysis at {h['ts']}: BTC=${h['price']:,.0f}, RSI={h['rsi']}, "
                    f"MACD={h['macd']}, Fear&Greed={h['fear_greed']}, "
                    f"Forecast={h['forecast']}"
                )
            })
            outcome_note = f" [Outcome: {h['outcome']}]" if h.get("outcome") else ""
            messages.append({
                "role": "assistant",
                "content": json.dumps({"action": h["action"], "reason": h["reason"]}) + outcome_note
            })

        # Add current analysis as the latest user message
        messages.append({"role": "user", "content": current_market})

        r    = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "system": system_prompt,
                "messages": messages
            },
            timeout=30
        )
        resp = r.json()
        if "content" not in resp:
            log(f"Errore API: {resp.get('error', resp)}")
            return {"action": "HOLD", "reason": "API error"}
        text  = resp["content"][0]["text"].strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return {"action": "HOLD", "reason": "AI error"}

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    log("AI Trading Bot su Hyperliquid avviato!")
    log(f"API address:  {account.address}")
    log(f"Main address: {HYPERLIQUID_ADDR}")
    log(f"Analisi ogni {INTERVAL//60} min | Monitoraggio ogni {MONITOR//60} min")
    log(f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}% | Trailing: {TRAIL_PCT*100}%")
    log(f"History: ultimi {MAX_HISTORY} cicli")

    notify(
        f"🚀 *Bot avviato!*\n"
        f"Simbolo: {SYMBOL} | Leva: {LEVERAGE}x\n"
        f"SL: {SL_PCT*100}% | TP: {TP_PCT*100}%\n"
        f"Analisi ogni {INTERVAL//60} min\n"
        f"Monitoraggio ogni {MONITOR//60} min"
    )

    last_analysis = 0

    while True:
        try:
            now = time.time()

            # ── MONITORAGGIO POSIZIONI ──
            log("--- Monitoraggio posizioni ---")
            position_closed = monitor_positions()

            if position_closed:
                log("Posizione chiusa! Analisi immediata...")
                last_analysis = 0

            # ── ANALISI COMPLETA ──
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
                    log(f"History: {len(decision_history)} cicli registrati")
                    log("AI in analisi...")

                    decision = ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions)
                    action   = decision.get("action", "HOLD")
                    reason   = decision.get("reason", "")
                    log(f"Decisione: {action}")
                    log(f"Motivo: {reason}")

                    # ── REGISTRA IN HISTORY ──
                    history_entry = {
                        "ts":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "price":      market["price"],
                        "rsi":        market["rsi"],
                        "macd":       market["macd"],
                        "fear_greed": fear_greed,
                        "forecast":   market["forecast"],
                        "action":     action,
                        "reason":     reason,
                        "outcome":    None  # filled later by monitor_positions
                    }
                    decision_history.append(history_entry)

                    if action == "BUY" and balance >= 10 and not positions:
                        success = place_order("BUY", market["price"], balance)
                        if success:
                            decision_history[-1]["outcome"] = f"Order opened @ ${market['price']:,.2f}"
                            last_analysis = now

                    elif action == "SELL" and balance >= 10 and not positions:
                        success = place_order("SELL", market["price"], balance)
                        if success:
                            decision_history[-1]["outcome"] = f"Order opened @ ${market['price']:,.2f}"
                            last_analysis = now

                    elif positions:
                        log("Posizione già aperta — monitoriamo")
                        pos = positions[0]
                        decision_history[-1]["outcome"] = f"Position already open, PnL=${pos['pnl']:.2f}"
                        notify(
                            f"📊 *Aggiornamento posizione*\n"
                            f"{pos['side']} BTC\n"
                            f"Entry: ${pos['entry']:,.2f}\n"
                            f"PnL: ${pos['pnl']:.2f} USDC\n"
                            f"Prezzo: ${market['price']:,.2f}\n"
                            f"RSI: {market['rsi']} | F&G: {fear_greed}"
                        )
                        last_analysis = now

                    else:
                        # HOLD senza posizione → rianalizza al prossimo ciclo
                        log("HOLD — rianalisi tra 5 minuti")
                        decision_history[-1]["outcome"] = "HOLD — no action taken"

            log(f"Prossimo ciclo tra {MONITOR//60} minuti")
            time.sleep(MONITOR)

        except KeyboardInterrupt:
            log("Bot fermato.")
            notify("🛑 Bot fermato.", important=True)
            break
        except Exception as e:
            log(f"[ERRORE CRITICO] {e}")
            notify(f"⚠️ Errore critico: {e}", important=True)
            time.sleep(60)

if __name__ == "__main__":
    main()
