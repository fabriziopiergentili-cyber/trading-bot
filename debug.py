"""
debug.py - Testa tutte le API del bot
Esegui: python debug.py
"""
import os
import time
import json
import requests
import math
from datetime import datetime
from eth_account import Account

# ─── CONFIGURAZIONE ───────────────────────────────────────────────────────────
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
HYPERLIQUID_KEY = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
NEWSAPI_KEY     = os.environ.get("NEWSAPI_KEY", "")
HL_URL          = "https://api.hyperliquid.xyz"

PASS = "✅"
FAIL = "❌"

def section(title):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")

def ok(msg):
    print(f"  {PASS} {msg}")

def err(msg):
    print(f"  {FAIL} {msg}")

# ─── 1. VARIABILI D'AMBIENTE ─────────────────────────────────────────────────
section("1. VARIABILI D'AMBIENTE")
if ANTHROPIC_KEY:
    ok(f"ANTHROPIC_API_KEY trovata (primi 20 char): {ANTHROPIC_KEY[:20]}...")
else:
    err("ANTHROPIC_API_KEY mancante!")

if HYPERLIQUID_KEY:
    ok(f"HYPERLIQUID_PRIVATE_KEY trovata (primi 10 char): {HYPERLIQUID_KEY[:10]}...")
else:
    err("HYPERLIQUID_PRIVATE_KEY mancante!")

if NEWSAPI_KEY:
    ok(f"NEWSAPI_KEY trovata (primi 10 char): {NEWSAPI_KEY[:10]}...")
else:
    err("NEWSAPI_KEY mancante!")

# ─── 2. INDIRIZZO WALLET ─────────────────────────────────────────────────────
section("2. WALLET HYPERLIQUID")
try:
    account = Account.from_key(HYPERLIQUID_KEY)
    address = account.address
    ok(f"Indirizzo wallet: {address}")
except Exception as e:
    err(f"Errore chiave privata: {e}")
    address = None

# ─── 3. BILANCIO HYPERLIQUID ─────────────────────────────────────────────────
section("3. BILANCIO HYPERLIQUID")
if address:
    try:
        r = requests.post(f"{HL_URL}/info",
                         json={"type": "clearinghouseState", "user": address},
                         timeout=15)
        data    = r.json()
        balance = float(data.get("marginSummary", {}).get("accountValue", 0))
        ok(f"Bilancio: ${balance:.2f} USDC")
        positions = data.get("assetPositions", [])
        active = [p for p in positions if float(p.get("position", {}).get("szi", 0)) != 0]
        ok(f"Posizioni aperte: {len(active)}")
        for p in active:
            pos = p.get("position", {})
            ok(f"  → {pos.get('coin')} size={pos.get('szi')} entry=${pos.get('entryPx')} pnl=${pos.get('unrealizedPnl')}")
    except Exception as e:
        err(f"Errore bilancio: {e}")

# ─── 4. PREZZO BTC ────────────────────────────────────────────────────────────
section("4. PREZZO BTC (Hyperliquid)")
try:
    r     = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
    mids  = r.json()
    price = float(mids.get("BTC", 0))
    ok(f"Prezzo BTC: ${price:,.2f}")
except Exception as e:
    err(f"Errore prezzo: {e}")

# ─── 5. CANDELE E INDICATORI ──────────────────────────────────────────────────
section("5. CANDELE E INDICATORI TECNICI")
try:
    r2 = requests.post(f"{HL_URL}/info",
                      json={"type": "candleSnapshot",
                            "req": {"coin": "BTC", "interval": "1h",
                                    "startTime": int(time.time()*1000) - 86400000*8}},
                      timeout=15)
    candles = r2.json()
    closes  = [float(c["c"]) for c in candles]
    ok(f"Candele ricevute: {len(candles)}")
    ok(f"Prezzo attuale (close): ${closes[-1]:,.2f}")

    if len(closes) >= 26:
        gains  = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
        avg_g  = sum(gains[-14:]) / 14
        avg_l  = sum(losses[-14:]) / 14
        rsi    = round(100 - (100 / (1 + avg_g / avg_l)), 2) if avg_l != 0 else 50
        ema12  = sum(closes[-12:]) / 12
        ema26  = sum(closes[-26:]) / 26
        macd   = round(ema12 - ema26, 2)
        ema20  = round(sum(closes[-20:]) / 20, 2)
        ok(f"RSI (14): {rsi}")
        ok(f"MACD: {macd}")
        ok(f"EMA20: ${ema20:,.2f}")
        if len(closes) >= 200:
            ema200 = round(sum(closes[-200:]) / 200, 2)
            ok(f"EMA200: ${ema200:,.2f}")
        else:
            err(f"EMA200: dati insufficienti ({len(closes)} candele, servono 200)")

        bb_closes = closes[-20:]
        bb_mean   = sum(bb_closes) / 20
        bb_std    = math.sqrt(sum((x - bb_mean)**2 for x in bb_closes) / 20)
        ok(f"Bollinger Upper: ${round(bb_mean + 2*bb_std, 2):,.2f}")
        ok(f"Bollinger Lower: ${round(bb_mean - 2*bb_std, 2):,.2f}")
    else:
        err(f"Candele insufficienti: {len(closes)} (servono almeno 26)")
except Exception as e:
    err(f"Errore candele: {e}")

# ─── 6. ORDER BOOK ────────────────────────────────────────────────────────────
section("6. ORDER BOOK")
try:
    r3      = requests.post(f"{HL_URL}/info", json={"type": "l2Book", "coin": "BTC"}, timeout=15)
    book    = r3.json()
    bids    = book.get("levels", [[]])[0][:5]
    asks    = book.get("levels", [[]])[1][:5]
    bid_vol = sum(float(b["sz"]) for b in bids)
    ask_vol = sum(float(a["sz"]) for a in asks)
    ok(f"Bid volume (top 5): {bid_vol:.4f} BTC")
    ok(f"Ask volume (top 5): {ask_vol:.4f} BTC")
    ok(f"Bid/Ask ratio: {round(bid_vol/ask_vol, 3) if ask_vol > 0 else 'N/A'}")
except Exception as e:
    err(f"Errore order book: {e}")

# ─── 7. OPEN INTEREST + FUNDING ───────────────────────────────────────────────
section("7. OPEN INTEREST + FUNDING RATE")
try:
    r4      = requests.post(f"{HL_URL}/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
    btc_ctx = r4.json()[1][0]
    oi      = float(btc_ctx.get("openInterest", 0))
    funding = float(btc_ctx.get("funding", 0)) * 100
    ok(f"Open Interest: {oi:,.2f} BTC")
    ok(f"Funding Rate: {funding:.4f}%")
except Exception as e:
    err(f"Errore OI/Funding: {e}")

# ─── 8. FEAR & GREED ─────────────────────────────────────────────────────────
section("8. FEAR & GREED INDEX")
try:
    r5 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    d  = r5.json()["data"][0]
    ok(f"Fear & Greed: {d['value']} ({d['value_classification']})")
except Exception as e:
    err(f"Errore Fear&Greed: {e}")

# ─── 9. WHALE ALERT ──────────────────────────────────────────────────────────
section("9. WHALE ALERT")
try:
    r6  = requests.get("https://api.whale-alert.io/v1/transactions",
                      params={"api_key": "free", "min_value": 1000000,
                              "start": int(time.time()) - 3600, "limit": 3},
                      timeout=10)
    data = r6.json()
    txs  = data.get("transactions", [])
    if txs:
        ok(f"Transazioni trovate: {len(txs)}")
        for t in txs:
            ok(f"  → {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})")
    else:
        ok(f"Nessun movimento balena nell'ultima ora (risposta: {data.get('result', 'ok')})")
except Exception as e:
    err(f"Errore Whale Alert: {e}")

# ─── 10. NEWS API ─────────────────────────────────────────────────────────────
section("10. NEWS API")
try:
    r7       = requests.get("https://newsapi.org/v2/everything",
                            params={"q": "bitcoin crypto", "language": "en",
                                    "sortBy": "publishedAt", "pageSize": 3,
                                    "apiKey": NEWSAPI_KEY},
                            timeout=15)
    articles = r7.json().get("articles", [])
    ok(f"Articoli trovati: {len(articles)}")
    for a in articles[:3]:
        ok(f"  → {a['title'][:70]}...")
except Exception as e:
    err(f"Errore NewsAPI: {e}")

# ─── 11. ANTHROPIC AI ────────────────────────────────────────────────────────
section("11. ANTHROPIC AI (Claude)")
try:
    r8 = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 50,
              "messages": [{"role": "user",
                            "content": 'Rispondi SOLO con: {"action":"HOLD","reason":"test ok"}'}]},
        timeout=30
    )
    resp = r8.json()
    if "content" in resp:
        text = resp["content"][0]["text"].strip()
        ok(f"Risposta AI: {text}")
    else:
        err(f"Errore AI: {resp.get('error', resp)}")
except Exception as e:
    err(f"Errore Anthropic: {e}")

# ─── RIEPILOGO ────────────────────────────────────────────────────────────────
section("RIEPILOGO")
print("  Debug completato! Controlla eventuali ❌ sopra.")
print(f"  Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
