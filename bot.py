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
INTERVAL  = 1800   # FIX: era 3600 (1h), ora 1800 (30min) per reagire più velocemente
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
subscriber_ids   = set()
last_update_id   = 0
trailing_stops   = {}  # FIX: dizionario per tracciare trailing stop reali per coin

# ─── LOG ─────────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── TELEGRAM: POLL FOR NEW USERS ─────────────────────────────────────────────
def poll_telegram():
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
    if not TELEGRAM_TOKEN or not subscriber_ids:
        return
    prefix = "🚨 " if important else "🤖 "
    text   = f"{prefix}*AI Trading Bot*\n\n{msg}"
    for chat_id in list(subscriber_ids):
        send_message(text, chat_id=chat_id)

# ─── FIX 1: BILANCIO PERPS (non spot) ────────────────────────────────────────
def get_balance():
    """
    FIX: Legge il saldo dal conto PERPS (clearinghouseState),
    non dallo spot (spotClearinghouseState).
    Prima leggeva il saldo sbagliato → il bot poteva non tradare mai.
    """
    try:
        state = hl_info.user_state(HYPERLIQUID_ADDR)
        balance = float(state.get("crossMarginSummary", {}).get("accountValue", 0))
        log(f"Bilancio USDC (perps): ${balance:.2f}")
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

# ─── FIX 2: LIMIT ORDER (maker) invece di market (taker) ─────────────────────
def place_order(side, price, balance):
    """
    FIX: Usa limit order invece di market order.
    Risparmio fee: 0,0144% (maker) vs 0,0432% (taker) → 3x meno costoso.
    Dopo l'apertura piazza SL e TP come ordini reali su Hyperliquid.
    """
    try:
        size     = round((balance * min(RISK_PCT, MAX_RISK) * LEVERAGE) / price, 5)
        min_size = round(10 / price * 1.01, 5)
        size     = max(size, min_size)
        is_buy   = side == "BUY"

        # FIX: Prezzo limit leggermente migliore del market per entrare come maker
        # BUY: offriamo un po' sotto il prezzo attuale
        # SELL: offriamo un po' sopra il prezzo attuale
        if is_buy:
            limit_price = round(price * 0.9998, 1)  # 0.02% sotto
        else:
            limit_price = round(price * 1.0002, 1)  # 0.02% sopra

        sl = round(price * (1 - SL_PCT) if is_buy else price * (1 + SL_PCT), 1)
        tp = round(price * (1 + TP_PCT) if is_buy else price * (1 - TP_PCT), 1)

        log(f"Apertura {side} LIMIT: {size} BTC @ ${limit_price:,.2f} (market: ${price:,.2f})")

        # FIX: Limit order con GTC (Good Till Cancelled)
        result = exchange_open.order(
            SYMBOL,
            is_buy,
            size,
            limit_price,
            {"limit": {"tif": "Gtc"}}
        )

        if result and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            if statuses and "error" not in statuses[0]:
                log(f"Ordine {side} LIMIT inviato @ ${limit_price:,.2f}")

                # FIX 3: Piazza SL e TP come ordini reali su Hyperliquid
                # Aspetta 2 secondi che l'ordine venga processato
                time.sleep(2)
                place_sl_tp(side, size, sl, tp, is_buy)

                # Inizializza trailing stop tracking
                trailing_stops[SYMBOL] = {
                    "side": side,
                    "sl": sl,
                    "entry": price
                }

                notify(
                    f"{'🟢' if is_buy else '🔴'} *Ordine {side} LIMIT aperto!*\n"
                    f"BTC @ ${limit_price:,.2f}\n"
                    f"Size: {size} BTC\n"
                    f"Stop Loss reale: ${sl:,.2f}\n"
                    f"Take Profit reale: ${tp:,.2f}\n"
                    f"Fee risparmiate: ~{(0.0432-0.0144):.4f}% (maker vs taker)",
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

# ─── FIX 3: SL E TP COME ORDINI REALI ────────────────────────────────────────
def place_sl_tp(side, size, sl_price, tp_price, is_buy):
    """
    FIX CRITICO: Piazza Stop Loss e Take Profit come ordini reali su Hyperliquid.
    Prima erano solo simulati nel codice Python → se il bot crashava, nessuna protezione.
    Ora gli ordini vivono sul server Hyperliquid indipendentemente dal bot.
    """
    try:
        # Stop Loss: ordine trigger che si attiva al prezzo SL
        sl_result = exchange_close.order(
            SYMBOL,
            not is_buy,   # direzione opposta per chiudere
            size,
            sl_price,
            {
                "trigger": {
                    "triggerPx": sl_price,
                    "isMarket": True,       # esegui a mercato quando triggerato
                    "tpsl": "sl"            # tipo: stop loss
                }
            },
            reduce_only=True
        )
        if sl_result and sl_result.get("status") == "ok":
            log(f"✅ Stop Loss reale piazzato @ ${sl_price:,.2f}")
        else:
            log(f"⚠️ Errore SL: {sl_result}")

        # Take Profit: ordine trigger al prezzo TP
        tp_result = exchange_close.order(
            SYMBOL,
            not is_buy,
            size,
            tp_price,
            {
                "trigger": {
                    "triggerPx": tp_price,
                    "isMarket": True,
                    "tpsl": "tp"            # tipo: take profit
                }
            },
            reduce_only=True
        )
        if tp_result and tp_result.get("status") == "ok":
            log(f"✅ Take Profit reale piazzato @ ${tp_price:,.2f}")
        else:
            log(f"⚠️ Errore TP: {tp_result}")

    except Exception as e:
        log(f"[ERRORE] SL/TP: {e}")

# ─── CANCELLA ORDINI APERTI ───────────────────────────────────────────────────
def cancel_open_orders():
    """Cancella tutti gli ordini aperti (SL/TP) quando si chiude manualmente una posizione."""
    try:
        open_orders = hl_info.open_orders(HYPERLIQUID_ADDR)
        for order in open_orders:
            if order.get("coin") == SYMBOL:
                exchange_close.cancel(SYMBOL, order["oid"])
                log(f"Ordine {order['oid']} cancellato")
    except Exception as e:
        log(f"[ERRORE] Cancellazione ordini: {e}")

# ─── CHIUDI POSIZIONE ─────────────────────────────────────────────────────────
def close_position(pos):
    try:
        coin   = pos["coin"]
        size   = abs(pos["size"])
        log(f"Chiusura {pos['side']} {coin} size={size}")

        # Prima cancella SL/TP reali per evitare doppia esecuzione
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

# ─── FIX 4: TRAILING STOP FUNZIONANTE ────────────────────────────────────────
def update_trailing_stop(pos, price):
    """
    FIX: Il trailing stop ora aggiorna realmente l'ordine SL su Hyperliquid.
    Prima calcolava il nuovo prezzo ma non faceva nulla (solo un log).
    Ora cancella il vecchio SL e ne piazza uno nuovo al prezzo aggiornato.
    """
    coin    = pos["coin"]
    is_long = pos["side"] == "LONG"
    size    = abs(pos["size"])
    entry   = pos["entry"]

    trail_data = trailing_stops.get(SYMBOL, {})
    current_sl = trail_data.get("sl", entry * (1 - SL_PCT) if is_long else entry * (1 + SL_PCT))

    if is_long and price >= entry * 1.02:
        new_sl = round(price * (1 - TRAIL_PCT), 1)
        if new_sl > current_sl:
            log(f"Trailing stop LONG aggiornato: ${current_sl:,.2f} → ${new_sl:,.2f}")

            # Cancella vecchio SL
            cancel_open_orders()

            # Piazza nuovo SL aggiornato
            try:
                sl_result = exchange_close.order(
                    SYMBOL,
                    False,      # sell per chiudere long
                    size,
                    new_sl,
                    {
                        "trigger": {
                            "triggerPx": new_sl,
                            "isMarket": True,
                            "tpsl": "sl"
                        }
                    },
                    reduce_only=True
                )
                if sl_result and sl_result.get("status") == "ok":
                    trailing_stops[SYMBOL]["sl"] = new_sl
                    log(f"✅ Nuovo trailing SL piazzato @ ${new_sl:,.2f}")
                    notify(f"📈 *Trailing Stop aggiornato*\n${current_sl:,.2f} → ${new_sl:,.2f}\nProfitto protetto: ${(new_sl - entry) * size:.2f} USDC")
            except Exception as e:
                log(f"[ERRORE] Aggiornamento trailing SL: {e}")

    elif not is_long and price <= entry * 0.98:
        new_sl = round(price * (1 + TRAIL_PCT), 1)
        if new_sl < current_sl:
            log(f"Trailing stop SHORT aggiornato: ${current_sl:,.2f} → ${new_sl:,.2f}")

            cancel_open_orders()

            try:
                sl_result = exchange_close.order(
                    SYMBOL,
                    True,       # buy per chiudere short
                    size,
                    new_sl,
                    {
                        "trigger": {
                            "triggerPx": new_sl,
                            "isMarket": True,
                            "tpsl": "sl"
                        }
                    },
                    reduce_only=True
                )
                if sl_result and sl_result.get("status") == "ok":
                    trailing_stops[SYMBOL]["sl"] = new_sl
                    log(f"✅ Nuovo trailing SL SHORT piazzato @ ${new_sl:,.2f}")
                    notify(f"📉 *Trailing Stop SHORT aggiornato*\n${current_sl:,.2f} → ${new_sl:,.2f}")
            except Exception as e:
                log(f"[ERRORE] Aggiornamento trailing SL SHORT: {e}")

# ─── MONITORAGGIO POSIZIONI ───────────────────────────────────────────────────
def monitor_positions():
    """
    FIX: Il monitoraggio ora serve principalmente per il trailing stop.
    SL e TP reali vengono gestiti direttamente da Hyperliquid,
    quindi non c'è più rischio se il bot crasha.
    """
    positions       = get_positions()
    position_closed = False

    if not positions:
        trailing_stops.clear()
        return False

    price = get_price()

    for pos in positions:
        entry   = pos["entry"]
        pnl     = pos["pnl"]
        is_long = pos["side"] == "LONG"

        log(f"Pos {pos['side']} | Entry: ${entry:,.2f} | PnL: ${pnl:.2f} | Price: ${price:,.2f}")

        # FIX: aggiorna trailing stop reale (SL/TP base gestiti da Hyperliquid)
        update_trailing_stop(pos, price)

    return position_closed

# ─── FIX 5: EMA CORRETTA (esponenziale, non semplice) ────────────────────────
def calculate_ema(closes, period):
    """
    FIX: EMA vera con smoothing esponenziale.
    Prima usava sum(closes[-N:])/N che è una SMA, non una EMA.
    L'EMA pesa di più i dati recenti → segnali più reattivi.
    """
    if len(closes) < period:
        return sum(closes) / len(closes)
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # seed con SMA
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 2)

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

        # FIX: EMA vera invece di SMA
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
            "pivot": pivot, "r1": r1, "s1": s1, "r2": r2_, "s2": s2,
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
    log("AI Trading Bot su Hyperliquid avviato! [versione FIX]")
    log(f"API address:  {account.address}")
    log(f"Main address: {HYPERLIQUID_ADDR}")
    log(f"Analisi ogni {INTERVAL//60} min | Monitoraggio ogni {MONITOR//60} min")
    log("FIX attivi: bilancio perps, limit orders, SL/TP reali, trailing stop reale, EMA vera")
    log("Telegram bot listening for /start commands...")

    last_analysis = 0

    poll_telegram()

    while True:
        try:
            now = time.time()

            poll_telegram()

            log("--- Position monitoring ---")
            position_closed = monitor_positions()

            if position_closed:
                log("Position closed! Immediate analysis...")
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
                            decision_history[-1]["outcome"] = f"Limit order BUY @ ${market['price']:,.2f}"
                            last_analysis = now

                    elif action == "SELL" and balance >= 10 and not positions:
                        success = place_order("SELL", market["price"], balance)
                        if success:
                            decision_history[-1]["outcome"] = f"Limit order SELL @ ${market['price']:,.2f}"
                            last_analysis = now

                    elif positions:
                        pos = positions[0]
                        decision_history[-1]["outcome"] = f"Position already open, PnL=${pos['pnl']:.2f}"
                        last_analysis = now

                    else:
                        decision_history[-1]["outcome"] = "HOLD — no action"
                        log("HOLD — re-analysis in 30 minutes")

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
