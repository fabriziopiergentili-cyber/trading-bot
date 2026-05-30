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
SL_PCT    = 0.012
TP_PCT    = 0.04
TRAIL_PCT = 0.0266
TRAILING_ENABLED = False
INTERVAL  = 1800
MONITOR   = 300
MAX_HISTORY  = 20
MAX_TRADES_STORED = 500
HISTORY_FILE = "/app/history.json"

HL_URL       = "https://api.hyperliquid.xyz"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else None

# ─── SETUP SDK ────────────────────────────────────────────────────────────────
account        = Account.from_key(HYPERLIQUID_KEY)
hl_info        = Info(constants.MAINNET_API_URL)
exchange_open  = Exchange(account, constants.MAINNET_API_URL)
exchange_close = Exchange(account, constants.MAINNET_API_URL, account_address=HYPERLIQUID_ADDR)

# ─── STATE ────────────────────────────────────────────────────────────────────
decision_history  = deque(maxlen=MAX_HISTORY)
subscriber_ids    = set()
last_update_id    = 0
trailing_stops    = {}
trade_history     = []
position_open_time = None
last_known_position = None
last_daily_report = None
DAILY_REPORT_HOUR = 8

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── MEMORIA PERSISTENTE ──────────────────────────────────────────────────────
def save_history():
    try:
        data = {
            "trade_history":    trade_history[-MAX_TRADES_STORED:],
            "decision_history": list(decision_history)
        }
        with open(HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[ERRORE] Save history: {e}")

def load_history():
    global trade_history
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
        trade_history = data.get("trade_history", [])[-MAX_TRADES_STORED:]
        for h in data.get("decision_history", []):
            decision_history.append(h)
        log(f"History caricata: {len(trade_history)} trade, {len(decision_history)} decisioni")
    except FileNotFoundError:
        log("Nessuna history precedente — si parte da zero")
    except Exception as e:
        log(f"[ERRORE] Load history: {e}")

# ─── STATS ───────────────────────────────────────────────────────────────────
def record_trade(side, entry, exit_price, pnl, reason="", duration_min=None):
    global trade_history
    trade_history.append({
        "ts":       datetime.now().strftime("%Y-%m-%d %H:%M"),
        "side":     side,
        "entry":    entry,
        "exit":     exit_price,
        "pnl":      round(pnl, 4),
        "reason":   reason,
        "duration": duration_min
    })
    # Troncamento in-place per evitare il bug della variabile locale
    if len(trade_history) > MAX_TRADES_STORED:
        trade_history[:] = trade_history[-MAX_TRADES_STORED:]
    dur_str = f" | Durata: {duration_min} min" if duration_min is not None else ""
    log(f"Trade registrato: {side} | Entry: ${entry:,.2f} | Exit: ${exit_price:,.2f} | PnL: ${pnl:.4f} | {reason}{dur_str}")
    save_history()

def get_stats():
    if not trade_history:
        return None
    wins      = [t for t in trade_history if t["pnl"] > 0]
    losses    = [t for t in trade_history if t["pnl"] < 0]
    breaks    = [t for t in trade_history if t["pnl"] == 0]
    total     = len(trade_history)
    win_rate  = round(len(wins) / total * 100, 1) if total > 0 else 0
    total_pnl = round(sum(t["pnl"] for t in trade_history), 4)
    best      = max(trade_history, key=lambda t: t["pnl"])
    worst     = min(trade_history, key=lambda t: t["pnl"])
    durations = [t["duration"] for t in trade_history if t.get("duration") is not None]
    avg_dur   = round(sum(durations) / len(durations)) if durations else None
    premature = [t for t in trade_history
                 if t.get("duration") is not None and t["duration"] < 90 and t["pnl"] < 0]
    return {
        "total": total, "wins": len(wins),
        "losses": len(losses), "breaks": len(breaks),
        "win_rate": win_rate, "total_pnl": total_pnl,
        "best": best["pnl"], "worst": worst["pnl"],
        "best_ts": best["ts"], "worst_ts": worst["ts"],
        "avg_dur": avg_dur, "premature": len(premature),
    }

def format_stats_message(balance):
    stats     = get_stats()
    positions = get_positions()
    price     = get_price()
    pos_str   = "Nessuna posizione aperta"
    if positions:
        pos = positions[0]
        pnl_emoji = "📈" if pos["pnl"] >= 0 else "📉"
        pos_str = (
            f"{pos['side']} BTC\n"
            f"Entry: ${pos['entry']:,.2f}\n"
            f"Prezzo: ${price:,.2f}\n"
            f"PnL: {pnl_emoji} ${pos['pnl']:.4f} USDC"
        )
    msg = (
        f"📊 *Performance Bot*\n\n"
        f"💰 *Saldo:* ${balance:.2f} USDC\n\n"
        f"📌 *Posizione aperta:*\n{pos_str}\n\n"
    )
    if not stats:
        msg += "⏳ Nessun trade chiuso ancora."
        return msg
    pnl_emoji = "📈" if stats["total_pnl"] >= 0 else "📉"
    wr_emoji  = "✅" if stats["win_rate"] >= 50 else "⚠️"
    msg += (
        f"🎯 *Trades chiusi:* {stats['total']}\n"
        f"✅ Vincenti: {stats['wins']}\n"
        f"❌ Perdenti: {stats['losses']}\n"
        f"➖ Pareggi: {stats['breaks']}\n\n"
        f"{wr_emoji} *Win Rate:* {stats['win_rate']}%\n"
        f"{pnl_emoji} *PnL totale:* ${stats['total_pnl']:+.4f} USDC\n"
        f"🏆 Miglior trade: ${stats['best']:+.4f} ({stats['best_ts']})\n"
        f"💀 Peggior trade: ${stats['worst']:+.4f} ({stats['worst_ts']})"
    )
    if stats.get("avg_dur") is not None:
        msg += f"\n⏱️ Durata media: {stats['avg_dur']} min"
        if stats["premature"] > 0:
            msg += f"\n⚠️ Uscite premature (<90min in perdita): {stats['premature']}"
    return msg

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def poll_telegram():
    global last_update_id
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 2},
            timeout=10
        )
        updates = r.json().get("result", [])
        for update in updates:
            last_update_id = update["update_id"]
            msg     = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text    = msg.get("text", "")
            if chat_id and text.startswith("/start"):
                if chat_id not in subscriber_ids:
                    subscriber_ids.add(chat_id)
                    log(f"New subscriber: {chat_id}")
                    send_message(
                        f"👋 Welcome to *AI Trading Bot*!\n\n"
                        f"Commands:\n"
                        f"/start — subscribe\n"
                        f"/stop — unsubscribe\n"
                        f"/status — stato rapido\n"
                        f"/stats — statistiche complete e saldo",
                        chat_id=chat_id
                    )
                else:
                    send_message("Already subscribed! ✅", chat_id=chat_id)
            elif chat_id and text.startswith("/stop"):
                subscriber_ids.discard(chat_id)
                send_message("Unsubscribed. Send /start to re-subscribe.", chat_id=chat_id)
            elif chat_id and text.startswith("/status"):
                balance   = get_balance()
                positions = get_positions()
                price     = get_price()
                pos_str   = "No open positions"
                if positions:
                    pos_str = "\n".join([
                        f"• {p['side']} BTC — Entry: ${p['entry']:,.2f} | PnL: ${p['pnl']:.4f}"
                        for p in positions
                    ])
                send_message(
                    f"📊 *Bot Status*\n\n"
                    f"BTC: ${price:,.2f}\n"
                    f"Balance: ${balance:.2f} USDC\n"
                    f"Positions:\n{pos_str}\n"
                    f"History: {len(decision_history)} cycles\n"
                    f"Subscribers: {len(subscriber_ids)}",
                    chat_id=chat_id
                )
            elif chat_id and text.startswith("/stats"):
                balance = get_balance()
                send_message(format_stats_message(balance), chat_id=chat_id)
    except Exception as e:
        log(f"[ERRORE] Telegram poll: {e}")

def send_message(text, chat_id=None, parse_mode="Markdown"):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        log(f"[ERRORE] Telegram send: {e}")

def notify(msg, important=False):
    if not TELEGRAM_TOKEN or not subscriber_ids:
        return
    prefix = "🚨 " if important else "🤖 "
    text   = f"{prefix}*AI Trading Bot*\n\n{msg}"
    for chat_id in list(subscriber_ids):
        send_message(text, chat_id=chat_id)

# ─── BILANCIO (corretto: clearinghouseState) ─────────────────────────────────
def get_balance():
    try:
        r = requests.post(f"{HL_URL}/info",
                          json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDR},
                          timeout=15)
        data = r.json()
        account_value = float(data.get("marginSummary", {}).get("accountValue", 0))
        log(f"Saldo conto: ${account_value:.2f}")
        return account_value
    except Exception as e:
        log(f"[ERRORE] Bilancio: {e}")
        return 0.0

# ─── POSIZIONI ────────────────────────────────────────────────────────────────
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

def get_price():
    try:
        r = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
        return float(r.json().get("BTC", 0))
    except:
        return 0.0

# ─── TICK SIZE HELPER ─────────────────────────────────────────────────────────
def round_to_tick(price, tick=0.5):
    return round(round(price / tick) * tick, 1)

# ─── CANCELLAZIONE SELETTIVA SOLO SL ─────────────────────────────────────────
def cancel_sl_order():
    """Cancella solo l'ordine stop loss (non il TP) per SYMBOL."""
    try:
        orders = hl_info.open_orders(HYPERLIQUID_ADDR)
        for order in orders:
            if order.get("coin") == SYMBOL:
                order_type = order.get("orderType", {})
                trigger = order_type.get("trigger", {})
                if trigger.get("tpsl") == "sl":
                    exchange_close.cancel(SYMBOL, order["oid"])
                    log(f"Cancellato SL order {order['oid']}")
    except Exception as e:
        log(f"[ERRORE] Cancel SL: {e}")

def cancel_open_orders():
    """Mantieni per compatibilità, ma ora usiamo cancel_sl_order."""
    cancel_sl_order()

# ─── ORDINE ───────────────────────────────────────────────────────────────────
def place_order(side, price, balance):
    try:
        size     = round((balance * min(RISK_PCT, MAX_RISK) * LEVERAGE) / price, 5)
        min_size = round(10 / price * 1.01, 5)
        size     = max(size, min_size)
        is_buy   = side == "BUY"

        raw_price   = price * 1.001 if is_buy else price * 0.999
        limit_price = round_to_tick(raw_price)
        sl          = round_to_tick(price * (1 - SL_PCT) if is_buy else price * (1 + SL_PCT))
        tp          = round_to_tick(price * (1 + TP_PCT)  if is_buy else price * (1 - TP_PCT))

        log(f"Apertura {side}: {size} BTC @ ${limit_price:,.1f} (market: ${price:,.1f})")
        result = exchange_open.order(SYMBOL, is_buy, size, limit_price, {"limit": {"tif": "Gtc"}})

        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" not in statuses[0]:
                log(f"Ordine {side} inviato @ ${limit_price:,.1f}")
                time.sleep(2)
                place_sl_tp(side, size, sl, tp, is_buy)
                trailing_stops[SYMBOL] = {"side": side, "sl": sl, "entry": price}
                global position_open_time
                position_open_time = time.time()
                notify(
                    f"{'🟢' if is_buy else '🔴'} *Ordine {side} aperto!*\n"
                    f"BTC @ ${limit_price:,.1f}\n"
                    f"Size: {size} BTC\n"
                    f"Stop Loss: ${sl:,.1f}\n"
                    f"Take Profit: ${tp:,.1f}",
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

# ─── SL E TP REALI ───────────────────────────────────────────────────────────
def place_sl_tp(side, size, sl_price, tp_price, is_buy):
    try:
        sl_result = exchange_close.order(
            SYMBOL, not is_buy, size, sl_price,
            {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True
        )
        if sl_result and sl_result.get("status") == "ok":
            log(f"✅ SL piazzato @ ${sl_price:,.1f}")
        else:
            log(f"⚠️ Errore SL: {sl_result}")

        tp_result = exchange_close.order(
            SYMBOL, not is_buy, size, tp_price,
            {"trigger": {"triggerPx": tp_price, "isMarket": True, "tpsl": "tp"}},
            reduce_only=True
        )
        if tp_result and tp_result.get("status") == "ok":
            log(f"✅ TP piazzato @ ${tp_price:,.1f}")
        else:
            log(f"⚠️ Errore TP: {tp_result}")
    except Exception as e:
        log(f"[ERRORE] SL/TP: {e}")

def close_position(pos):
    try:
        coin = pos["coin"]
        size = abs(pos["size"])
        log(f"Chiusura {pos['side']} {coin} size={size}")
        cancel_open_orders()
        result = exchange_close.market_close(coin, sz=size)
        if result and result.get("status") == "ok":
            log(f"Posizione {coin} chiusa!")
            trailing_stops.pop(SYMBOL, None)
            return True
        else:
            log(f"Errore chiusura: {result}")
            return False
    except Exception as e:
        log(f"[ERRORE] Chiusura: {e}")
        return False

def update_trailing_stop(pos, price):
    is_long    = pos["side"] == "LONG"
    size       = abs(pos["size"])
    entry      = pos["entry"]
    trail_data = trailing_stops.get(SYMBOL, {})
    current_sl = trail_data.get("sl", entry * (1 - SL_PCT) if is_long else entry * (1 + SL_PCT))

    if is_long and price >= entry * 1.02:
        new_sl = round_to_tick(price * (1 - TRAIL_PCT))
        if new_sl > current_sl:
            log(f"Trailing LONG: ${current_sl:,.1f} → ${new_sl:,.1f}")
            cancel_sl_order()
            try:
                r = exchange_close.order(
                    SYMBOL, False, size, new_sl,
                    {"trigger": {"triggerPx": new_sl, "isMarket": True, "tpsl": "sl"}},
                    reduce_only=True
                )
                if r and r.get("status") == "ok":
                    trailing_stops[SYMBOL]["sl"] = new_sl
                    notify(f"📈 *Trailing Stop aggiornato*\n${current_sl:,.1f} → ${new_sl:,.1f}")
            except Exception as e:
                log(f"[ERRORE] Trailing SL: {e}")

    elif not is_long and price <= entry * 0.98:
        new_sl = round_to_tick(price * (1 + TRAIL_PCT))
        if new_sl < current_sl:
            log(f"Trailing SHORT: ${current_sl:,.1f} → ${new_sl:,.1f}")
            cancel_sl_order()
            try:
                r = exchange_close.order(
                    SYMBOL, True, size, new_sl,
                    {"trigger": {"triggerPx": new_sl, "isMarket": True, "tpsl": "sl"}},
                    reduce_only=True
                )
                if r and r.get("status") == "ok":
                    trailing_stops[SYMBOL]["sl"] = new_sl
                    notify(f"📉 *Trailing Stop SHORT aggiornato*\n${current_sl:,.1f} → ${new_sl:,.1f}")
            except Exception as e:
                log(f"[ERRORE] Trailing SL SHORT: {e}")

def monitor_positions():
    global last_known_position, position_open_time
    positions = get_positions()

    if not positions:
        if last_known_position is not None:
            prev  = last_known_position
            price = get_price()
            if prev["side"] == "LONG":
                est_pnl = (price - prev["entry"]) * abs(prev["size"])
            else:
                est_pnl = (prev["entry"] - price) * abs(prev["size"])
            reason = "TP HIT" if est_pnl >= 0 else "SL HIT"
            dur    = round((time.time() - position_open_time) / 60) if position_open_time else None
            record_trade(prev["side"], prev["entry"], price, est_pnl, reason, dur)
            notify(
                f"{'✅' if est_pnl >= 0 else '🔴'} *{reason}*\n"
                f"{prev['side']} BTC\n"
                f"Entry: ${prev['entry']:,.1f} → ${price:,.1f}\n"
                f"PnL: ${est_pnl:.4f} USDC"
                + (f"\nDurata: {dur} min" if dur is not None else ""),
                important=True
            )
            last_known_position = None
            position_open_time  = None
            cancel_open_orders()
            trailing_stops.clear()
            return True
        trailing_stops.clear()
        return False

    price = get_price()
    for pos in positions:
        log(f"Pos {pos['side']} | Entry: ${pos['entry']:,.1f} | PnL: ${pos['pnl']:.4f} | Price: ${price:,.1f}")
        last_known_position = pos
        if TRAILING_ENABLED:
            update_trailing_stop(pos, price)
    return False

# ─── INDICATORI (con controllo dati minimi) ──────────────────────────────────
def calculate_ema(closes, period):
    if len(closes) < period:
        return sum(closes) / len(closes)
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

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
            log("Dati insufficienti (<26 candele)")
            return {}
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = round(100 - (100 / (1 + avg_g / avg_l)), 2) if avg_l != 0 else 50
        ema12  = calculate_ema(closes, 12)
        ema26  = calculate_ema(closes, 26)
        ema20  = calculate_ema(closes, 20)
        ema200 = calculate_ema(closes, 200) if len(closes) >= 200 else calculate_ema(closes, len(closes))
        macd   = round(ema12 - ema26, 2)
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
        r2_   = round(pivot + (last_high - last_low), 2)
        s2    = round(pivot - (last_high - last_low), 2)
        trs = [max(highs[-i]-lows[-i], abs(highs[-i]-closes[-i-1]), abs(lows[-i]-closes[-i-1]))
               for i in range(1, min(15, len(closes)))]
        atr = round(sum(trs) / len(trs), 2) if trs else 0
        if len(closes) >= 25:
            trs24 = [max(highs[-i]-lows[-i], abs(highs[-i]-closes[-i-1]), abs(lows[-i]-closes[-i-1]))
                     for i in range(1, 25)]
            atr_24h_avg = round(sum(trs24) / len(trs24), 2)
        else:
            atr_24h_avg = atr
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
        log(f"Pivot: {pivot} | R1: {r1} | S1: {s1} | ATR: {atr} | ATR24hAvg: {atr_24h_avg}")
        return {
            "price": price, "change24h": change24h,
            "rsi": rsi, "macd": macd,
            "ema20": ema20, "ema200": ema200,
            "bb_upper": bb_upper, "bb_lower": bb_lower,
            "pivot": pivot, "r1": r1, "s1": s1, "r2": r2_, "s2": s2,
            "atr": atr, "atr_24h_avg": atr_24h_avg, "order_book": order_book, "forecast": forecast,
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
    if not NEWSAPI_KEY:
        return "News API key mancante"
    try:
        r        = requests.get("https://newsapi.org/v2/everything",
                                params={"q": "bitcoin crypto elon musk trump",
                                        "language": "en", "sortBy": "publishedAt",
                                        "pageSize": 5, "apiKey": NEWSAPI_KEY},
                                timeout=15)
        articles = r.json().get("articles", [])
        return "\n".join([f"- {a['title']}" for a in articles[:5]]) or "No news"
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
                                   "start": int(time.time()) - 3600, "limit": 3},
                           timeout=10)
        txs = r.json().get("transactions", [])
        if not txs:
            return "No whale movements"
        return "\n".join([f"- {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})"
                          for t in txs[:3]])
    except:
        return "N/A"

def build_history_string():
    if not decision_history:
        return "No history yet."
    lines = ["HISTORY (last cycles):"]
    for i, h in enumerate(list(decision_history)[-10:]):
        outcome = h.get("outcome", "pending") or "pending"
        lines.append(f"[{i+1}] {h['ts']} BTC:${h['price']:,.0f} RSI:{h['rsi']} → {h['action']} | {outcome}")
    recent      = list(decision_history)[-5:]
    hold_streak = sum(1 for h in recent if h["action"] == "HOLD")
    if hold_streak >= 3:
        lines.append(f"⚠️ HOLD streak: {hold_streak} cycles. Must act or justify.")
    return "\n".join(lines)

def ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions):
    try:
        pos_str = "No open positions"
        if positions:
            pos_str = "\n".join([
                f"- {p['side']} {p['coin']}: size={p['size']}, entry=${p['entry']:,.2f}, PnL=${p['pnl']:.4f}"
                for p in positions
            ])

        system_prompt = """You are a disciplined AI trading agent on Hyperliquid. Quality over quantity: fewer, higher-probability trades.

ENTRY RULES — LONG (BUY): ALL must be true:
- RSI < 40
- Price ABOVE EMA20
- Funding rate <= 0.01%
- ATR > 24h average ATR (avoid flat/range markets)

ENTRY RULES — SHORT (SELL): ALL must be true:
- RSI > 60
- Price BELOW EMA20
- Funding rate >= -0.01%
- ATR > 24h average ATR

CLOSE RULES:
- NEVER use CLOSE if open position profit is below +2%. Let SL/TP handle it.
- Only CLOSE if profit >= +2% AND clear reversal signal.

GENERAL:
- If conditions are not ALL met, respond HOLD and wait. Do not force trades.
- It is OK to HOLD many cycles if no clean setup exists.
- Keep reason under 50 words.
- Respond ONLY with valid JSON: {"action":"BUY","reason":"..."} or SELL or CLOSE or HOLD."""

        current_market = (
            f"BTC: ${market.get('price'):,.2f} | RSI: {market.get('rsi')} | MACD: {market.get('macd')}\n"
            f"EMA20: ${market.get('ema20'):,.2f} | EMA200: ${market.get('ema200'):,.2f}\n"
            f"BB: [{market.get('bb_lower'):,.2f} - {market.get('bb_upper'):,.2f}]\n"
            f"Pivot: {market.get('pivot')} | R1: {market.get('r1')} | S1: {market.get('s1')}\n"
            f"ATR: {market.get('atr')} (24h avg: {market.get('atr_24h_avg')}) | {market.get('order_book')}\n"
            f"Forecast: {market.get('forecast')}\n"
            f"{oi_funding} | Fear&Greed: {fear_greed}\n"
            f"Whale: {whale}\n"
            f"News: {news[:200]}\n\n"
            f"Balance: ${balance:.2f} | Leverage: {LEVERAGE}x | Positions: {pos_str}\n\n"
            f"{build_history_string()}\n\n"
            f"Decision (JSON only):"
        )

        messages = []
        for h in list(decision_history)[-4:-1]:
            messages.append({
                "role": "user",
                "content": f"BTC=${h['price']:,.0f} RSI={h['rsi']} MACD={h['macd']}"
            })
            outcome_note = f" [Outcome: {h['outcome']}]" if h.get("outcome") else ""
            messages.append({
                "role": "assistant",
                "content": json.dumps({"action": h["action"], "reason": h["reason"][:100]}) + outcome_note
            })
        messages.append({"role": "user", "content": current_market})

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system":     system_prompt,
                "messages":   messages
            },
            timeout=30
        )
        resp = r.json()
        if "content" not in resp:
            log(f"API error: {resp.get('error', resp)}")
            return {"action": "HOLD", "reason": "API error"}
        text  = resp["content"][0]["text"].strip()
        start = text.find("{")
        end   = text.rfind("}") + 1
        log(f"AI response: {text}")
        return json.loads(text[start:end])
    except Exception as e:
        log(f"[ERRORE] AI: {e}")
        return {"action": "HOLD", "reason": "AI error"}

# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def main():
    global last_daily_report, position_open_time
    log("AI Trading Bot avviato! [v5 — filtri rafforzati, SL/TP 1.2/4%, tracking durata]")
    log(f"API address:  {account.address}")
    log(f"Main address: {HYPERLIQUID_ADDR}")
    log(f"Analisi ogni {INTERVAL//60} min | Monitoraggio ogni {MONITOR//60} min")

    load_history()

    last_analysis = 0
    poll_telegram()

    while True:
        try:
            now    = time.time()
            now_dt = datetime.now()

            poll_telegram()

            if (now_dt.hour == DAILY_REPORT_HOUR and
                    now_dt.strftime("%Y-%m-%d") != last_daily_report):
                balance = get_balance()
                notify(f"🌅 *Report Giornaliero — {now_dt.strftime('%d/%m/%Y')}*\n\n" + format_stats_message(balance))
                last_daily_report = now_dt.strftime("%Y-%m-%d")
                log("Report giornaliero inviato")

            log("--- Position monitoring ---")
            position_closed = monitor_positions()
            if position_closed:
                last_analysis = 0

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
                    log("Data unavailable, retry in 5 min")
                else:
                    log(f"Fear&Greed: {fear_greed} | History: {len(decision_history)} | Subscribers: {len(subscriber_ids)}")
                    log("AI analysing...")

                    decision = ai_decision(market, news, fear_greed, whale, oi_funding, balance, positions)
                    action   = decision.get("action", "HOLD")
                    reason   = decision.get("reason", "")
                    log(f"Decision: {action} | {reason}")

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
                    save_history()

                    action_emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "🔒" if action == "CLOSE" else "⏸️"
                    notify(
                        f"{action_emoji} *Decision: {action}*\n\n"
                        f"BTC: ${market['price']:,.2f} | RSI: {market['rsi']}\n"
                        f"Fear&Greed: {fear_greed}\n"
                        f"_{reason}_",
                        important=(action != "HOLD")
                    )

                    if action == "BUY" and balance >= 10 and not positions:
                        success = place_order("BUY", market["price"], balance)
                        if success:
                            decision_history[-1]["outcome"] = f"BUY @ ${market['price']:,.2f}"
                            save_history()
                            last_analysis = now

                    elif action == "SELL" and balance >= 10 and not positions:
                        success = place_order("SELL", market["price"], balance)
                        if success:
                            decision_history[-1]["outcome"] = f"SELL @ ${market['price']:,.2f}"
                            save_history()
                            last_analysis = now

                    elif action == "CLOSE" and positions:
                        pos     = positions[0]
                        success = close_position(pos)
                        if success:
                            dur = round((time.time() - position_open_time) / 60) if position_open_time else None
                            record_trade(pos["side"], pos["entry"], market["price"], pos["pnl"], "AI CLOSE", dur)
                            position_open_time = None
                            decision_history[-1]["outcome"] = f"CLOSED @ ${market['price']:,.2f} PnL=${pos['pnl']:.4f}"
                            save_history()
                            notify("🔒 *Posizione chiusa da AI*\n" + reason, important=True)
                            last_analysis = 0

                    elif positions:
                        pos = positions[0]
                        decision_history[-1]["outcome"] = f"Position open PnL=${pos['pnl']:.4f}"
                        save_history()
                        last_analysis = now

                    else:
                        decision_history[-1]["outcome"] = "HOLD"
                        save_history()
                        log("HOLD — re-analysis in 30 min")

            log(f"Next cycle in {MONITOR//60} min")
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
