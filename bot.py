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

SYMBOL    = "BTC"
LEVERAGE  = 2
RISK_PCT  = 0.02
MAX_RISK  = 0.05
SL_PCT    = 0.015
TP_PCT    = 0.03
TRAIL_PCT = 0.0266
INTERVAL  = 3600
MONITOR   = 300
MAX_HISTORY = 20

HL_URL       = "https://api.hyperliquid.xyz"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ─── SETUP SDK ────────────────────────────────────────────────────────────────
account        = Account.from_key(HYPERLIQUID_KEY)
hl_info        = Info(constants.MAINNET_API_URL)
exchange_open  = Exchange(account, constants.MAINNET_API_URL)
exchange_close = Exchange(account, constants.MAINNET_API_URL, account_address=HYPERLIQUID_ADDR)

# ─── STATE ────────────────────────────────────────────────────────────────────
decision_history = deque(maxlen=MAX_HISTORY)
subscriber_ids   = set()   # all chat IDs that have /start-ed the bot
last_update_id   = 0       # for Telegram long polling

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── TELEGRAM: POLL FOR NEW USERS ─────────────────────────────────────────────
def poll_telegram():
    """Check for new /start messages and register users."""
    global last_update_id
    try:
        r = requests.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 2},
            timeout=10
        )
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text    = msg.get("text", "")
            if chat_id and text.startswith("/start"):
                if chat_id not in subscriber_ids:
                    subscriber_ids.add(chat_id)
                    log(f"New subscriber: {chat_id} (total: {len(subscriber_ids)})")
                    # Welcome message
                    send_message(
                        f"👋 Welcome to *AI Trading Bot*!\n\n"
                        f"You will now receive all trading updates, decisions and alerts.\n\n"
                        f"Commands:\n"
                        f"/start — subscribe to updates\n"
                        f"/stop — unsubscribe\n"
                        f"/status — get current bot status",
                        chat_id=chat_id
                    )
                else:
                    send_message("You are already subscribed! ✅", chat_id=chat_id)

            elif chat_id and text.startswith("/stop"):
                subscriber_ids.discard(chat_id)
                send_message("You have unsubscribed. Send /start to re-subscribe.", chat_id=chat_id)

            elif chat_id and text.startswith("/status"):
                balance   = get_balance()
                positions = get_positions()
                price     = get_price()
                pos_str   = "No open positions"
                if positions:
                    pos_str = "\n".join([
                        f"• {p['side']} BTC — Entry: ${p['entry']:,.2f} | PnL: ${p['pnl']:.2f}"
                        for p in positions
                    ])
                send_message(
                    f"📊 *Bot Status*\n\n"
                    f"BTC Price: ${price:,.2f}\n"
                    f"Balance: ${balance:.2f} USDC\n"
                    f"Positions:\n{pos_str}\n"
                    f"History: {len(decision_history)} cycles\n"
                    f"Subscribers: {len(subscriber_ids)}",
                    chat_id=chat_id
                )
    except Exception as e:
        log(f"[ERRORE] Telegram poll: {e}")

# ─── TELEGRAM: SEND TO ONE ────────────────────────────────────────────────────
def send_message(text, chat_id=None, parse_mode="Markdown"):
    """Send a message to a specific chat_id."""
    try:
        requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        log(f"[ERRORE] Telegram send: {e}")

# ─── TELEGRAM: BROADCAST TO ALL ───────────────────────────────────────────────
def notify(msg, important=False):
    """Broadcast a message to all subscribers."""
    if not TELEGRAM_TOKEN or not subscriber_ids:
        return
    prefix = "🚨 " if important else "🤖 "
    text   = f"{prefix}*AI Trading Bot*\n\n{msg}"
    for chat_id in list(subscriber_ids):
        send_message(text, chat_id=chat_id)

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
                    f"{'🟢' if is_buy else '🔴'} *Order {side} opened!*\n"
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
        log(f"Chiusura {pos['side']} {coin} size={size}")
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

        log(f"Pos {pos['side']} | Entry: ${entry:,.2f} | PnL: ${pnl:.2f} | Price: ${price:,.2f}")

        if (is_long and price <= sl) or (not is_long and price >= sl):
            log("STOP LOSS scattato!")
            if close_position(pos):
                position_closed = True
                if decision_history:
                    decision_history[-1]["outcome"] = f"STOP LOSS @ ${price:,.2f} | PnL: ${pnl:.2f}"
                notify(
                    f"🔴 *STOP LOSS hit!*\n"
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
                    f"✅ *TAKE PROFIT hit!*\n"
                    f"{pos['side']} BTC\n"
                    f"Entry: ${entry:,.2f} → ${price:,.2f}\n"
                    f"PnL: ${pnl:.2f} USDC",
                    important=True
                )

        elif is_long and price >= entry * 1.02:
            new_sl = price * (1 - TRAIL_PCT)
            if new_sl > sl:
                log(f"Trailing stop → ${new_sl:,.2f}")
                notify(f"📈 Trailing stop updated: ${new_sl:,.2f}")
        elif not is_long and price <= entry * 0.98:
            new_sl = price * (1 + TRAIL_PCT)
            if new_sl < sl:
                log(f"Trailing stop → ${new_sl:,.2f}")
                notify(f"📉 Trailing stop updated: ${new_sl:,.2f}")
        else:
            log("Position running — no action needed")

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
        return "\n".join([f"- {a['title']}" for a in articles[:8]]) or "No news"
    except:
        return "No news"

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
            return "No whale movements"
        return "\n".join([f"- {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})"
                          for t in txs[:3]])
    except:
        return "Whale Alert N/A"

# ─── BUILD HISTORY STRING ─────────────────────────────────────────────────────
def build_history_string():
    if not decision_history:
        return "No decision history yet — this is the first analysis."
    lines = ["DECISION HISTORY (oldest to newest):"]
    for i, h in enumerate(decision_history):
        outcome = h.get("outcome", "pending")
        lines.append(
            f"[{i+1}] {h['ts']} | BTC: ${h['price']:,.0f} | RSI: {h['rsi']} | "
            f"Action: {h['action']} | Outcome: {outcome}"
        )
    recent     = list(decision_history)[-5:]
    hold_streak = sum(1 for h in recent if h["action"] == "HOLD")
    if hold_streak >= 3:
        lines.append(f"\n⚠️ WARNING: HOLD for {hold_streak} consecutive cycles. Consider acting.")
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

        system_prompt = """You are an expert AI trading agent on Hyperliquid.
Your goal is to maximize returns while managing risk carefully.

CRITICAL RULES:
1. Learn from your decision history — if you keep saying HOLD, you are missing opportunities.
2. If HOLD for 3+ consecutive cycles, you MUST find a reason to act or clearly explain why the market is untradeable.
3. RSI < 35 with any sign of recovery → strongly consider BUY.
4. RSI > 65 with weakening trend → strongly consider SELL.
5. Do NOT be overly cautious. A bot that never trades is useless.
6. Learn from outcomes: stop loss hit → be more careful next time. Take profit hit → similar setups are valid.
7. Always respond ONLY with valid JSON."""

        current_market = (
            f"=== CURRENT MARKET ===\n"
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
            f"=== {build_history_string()} ===\n\n"
            f"What is your trading decision? Respond ONLY with JSON:\n"
            f'{{"action":"BUY","reason":"..."}}\n'
            f'{{"action":"SELL","reason":"..."}}\n'
            f'{{"action":"HOLD","reason":"..."}}'
        )

        # Build multi-turn messages from history
        messages = []
        for h in list(decision_history)[:-1]:
            messages.append({
                "role": "user",
                "content": (
                    f"Analysis at {h['ts']}: BTC=${h['price']:,.0f}, RSI={h['rsi']}, "
                    f"MACD={h['macd']}, Fear&Greed={h['fear_greed']}, Forecast={h['forecast']}"
                )
            })
            outcome_note = f" [Outcome: {h['outcome']}]" if h.get("outcome") else ""
            messages.append({
                "role": "assistant",
                "content": json.dumps({"action": h["action"], "reason": h["reason"]}) + outcome_note
            })

        messages.append({"role": "user", "content": current_market})

        r    = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                  "system": system_prompt, "messages": messages},
            timeout=30
        )
        resp = r.json()
        if "content" not in resp:
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
    log("Telegram bot listening for /start commands...")

    last_analysis = 0

    # Initial poll to pick up any existing subscribers
    poll_telegram()

    while True:
        try:
            now = time.time()

            # ── POLL TELEGRAM FOR NEW USERS ──
            poll_telegram()

            # ── MONITORAGGIO POSIZIONI ──
            log("--- Position monitoring ---")
            position_closed = monitor_positions()

            if position_closed:
                log("Position closed! Immediate analysis...")
                last_analysis = 0

            # ── ANALISI COMPLETA ──
            if now - last_analysis >= INTERVAL:
                log("=" * 60)
                log("FULL ANALYSIS")

                market     = get_market_data()
                balance    = get_balance()
                positions  = get_positions()
                news       = get_news()
                fear_greed = get_fear_greed()
                whale      = get_whale_alert()
                oi_funding = get_oi_funding()

                if not market:
                    log("Data unavailable, retrying in 5 min")
                else:
                    log(f"Fear&Greed: {fear_greed}")
                    log(f"History: {len(decision_history)} cycles | Subscribers: {len(subscriber_ids)}")
                    log("AI analysing...")

                    decision = ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions)
                    action   = decision.get("action", "HOLD")
                    reason   = decision.get("reason", "")
                    log(f"Decision: {action}")
                    log(f"Reason: {reason}")

                    # ── REGISTER IN HISTORY ──
                    decision_history.append({
                        "ts":         datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "price":      market["price"],
                        "rsi":        market["rsi"],
                        "macd":       market["macd"],
                        "fear_greed": fear_greed,
                        "forecast":   market["forecast"],
                        "action":     action,
                        "reason":     reason,
                        "outcome":    None
                    })

                    # ── BROADCAST ANALYSIS TO ALL SUBSCRIBERS ──
                    action_emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⏸️"
                    notify(
                        f"{action_emoji} *Decision: {action}*\n\n"
                        f"BTC: ${market['price']:,.2f} | RSI: {market['rsi']}\n"
                        f"Fear&Greed: {fear_greed}\n"
                        f"Forecast: {market['forecast']}\n\n"
                        f"_{reason}_",
                        important=(action != "HOLD")
                    )

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
                        pos = positions[0]
                        decision_history[-1]["outcome"] = f"Position already open, PnL=${pos['pnl']:.2f}"
                        last_analysis = now

                    else:
                        decision_history[-1]["outcome"] = "HOLD — no action"
                        log("HOLD — re-analysis in 5 minutes")

            log(f"Next cycle in {MONITOR//60} minutes")
            time.sleep(MONITOR)

        except KeyboardInterrupt:
            log("Bot stopped.")
            notify("🛑 Bot stopped.", important=True)
            break
        except Exception as e:
            log(f"[CRITICAL ERROR] {e}")
            notify(f"⚠️ Critical error: {e}", important=True)
            time.sleep(60)

if __name__ == "__main__":
    main()
