"""
debug.py - Testa tutte le API del bot
"""
import os
import time
import json
import requests
import math
from datetime import datetime
from eth_account import Account

ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
HYPERLIQUID_KEY  = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")
HYPERLIQUID_ADDR = os.environ.get("HYPERLIQUID_ADDRESS", "")
NEWSAPI_KEY      = os.environ.get("NEWSAPI_KEY", "")
HL_URL           = "https://api.hyperliquid.xyz"

PASS = "✅"
FAIL = "❌"

def section(title):
    print(f"\n{'='*50}\n  {title}\n{'='*50}")

def ok(msg):  print(f"  {PASS} {msg}")
def err(msg): print(f"  {FAIL} {msg}")

# 1. ENV VARS
section("1. VARIABILI D'AMBIENTE")
ok(f"ANTHROPIC_API_KEY: {ANTHROPIC_KEY[:20]}...") if ANTHROPIC_KEY else err("ANTHROPIC_API_KEY mancante!")
ok(f"HYPERLIQUID_PRIVATE_KEY: {HYPERLIQUID_KEY[:10]}...") if HYPERLIQUID_KEY else err("HYPERLIQUID_PRIVATE_KEY mancante!")
ok(f"HYPERLIQUID_ADDRESS: {HYPERLIQUID_ADDR}") if HYPERLIQUID_ADDR else err("HYPERLIQUID_ADDRESS mancante!")
ok(f"NEWSAPI_KEY: {NEWSAPI_KEY[:10]}...") if NEWSAPI_KEY else err("NEWSAPI_KEY mancante!")

# 2. WALLET
section("2. WALLET (eth_account)")
try:
    account = Account.from_key(HYPERLIQUID_KEY)
    ok(f"Indirizzo derivato dalla chiave: {account.address}")
    ok(f"Indirizzo configurato (env):     {HYPERLIQUID_ADDR}")
    if account.address.lower() != HYPERLIQUID_ADDR.lower():
        err("ATTENZIONE: i due indirizzi sono DIVERSI — verifica HYPERLIQUID_ADDRESS!")
    else:
        ok("Gli indirizzi coincidono!")
except Exception as e:
    err(f"Errore chiave privata: {e}")

# 3. BILANCIO SPOT
section("3. BILANCIO SPOT (spotClearinghouseState)")
try:
    r    = requests.post(f"{HL_URL}/info",
                         json={"type": "spotClearinghouseState", "user": HYPERLIQUID_ADDR},
                         timeout=15)
    data = r.json()
    ok(f"Risposta ricevuta")
    balances = data.get("balances", [])
    for b in balances:
        if float(b.get("total", 0)) > 0:
            ok(f"  {b['coin']}: {b['total']} (hold: {b['hold']})")
    usdc = next((b for b in balances if b["coin"] == "USDC"), None)
    if usdc:
        ok(f"USDC disponibile: ${float(usdc['total']):.4f}")
    else:
        err("Nessun USDC trovato!")
except Exception as e:
    err(f"Errore bilancio spot: {e}")

# 4. POSIZIONI PERP
section("4. POSIZIONI PERP (clearinghouseState)")
try:
    r    = requests.post(f"{HL_URL}/info",
                         json={"type": "clearinghouseState", "user": HYPERLIQUID_ADDR},
                         timeout=15)
    data = r.json()
    ok(f"Account value: ${data.get('marginSummary', {}).get('accountValue', 0)}")
    active = [p for p in data.get("assetPositions", [])
              if float(p.get("position", {}).get("szi", 0)) != 0]
    ok(f"Posizioni aperte: {len(active)}")
    for p in active:
        pos = p["position"]
        ok(f"  {pos['coin']}: size={pos['szi']} entry=${pos['entryPx']} pnl=${pos['unrealizedPnl']}")
except Exception as e:
    err(f"Errore posizioni: {e}")

# 5. PREZZO BTC
section("5. PREZZO BTC")
try:
    r     = requests.post(f"{HL_URL}/info", json={"type": "allMids"}, timeout=15)
    price = float(r.json().get("BTC", 0))
    ok(f"Prezzo BTC: ${price:,.2f}")
except Exception as e:
    err(f"Errore prezzo: {e}")

# 6. CANDELE + INDICATORI
section("6. CANDELE E INDICATORI")
try:
    r2      = requests.post(f"{HL_URL}/info",
                            json={"type": "candleSnapshot",
                                  "req": {"coin": "BTC", "interval": "1h",
                                          "startTime": int(time.time()*1000) - 86400000*8}},
                            timeout=15)
    candles = r2.json()
    closes  = [float(c["c"]) for c in candles]
    ok(f"Candele ricevute: {len(candles)}")
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
            err(f"EMA200: dati insufficienti ({len(closes)}/200 candele)")
        bb_closes = closes[-20:]
        bb_mean   = sum(bb_closes) / 20
        bb_std    = math.sqrt(sum((x-bb_mean)**2 for x in bb_closes) / 20)
        ok(f"BB Upper: ${round(bb_mean+2*bb_std,2):,.2f} | Lower: ${round(bb_mean-2*bb_std,2):,.2f}")
    else:
        err(f"Candele insufficienti: {len(closes)}")
except Exception as e:
    err(f"Errore candele: {e}")

# 7. ORDER BOOK
section("7. ORDER BOOK")
try:
    r3      = requests.post(f"{HL_URL}/info", json={"type": "l2Book", "coin": "BTC"}, timeout=15)
    book    = r3.json()
    bids    = book.get("levels", [[]])[0][:5]
    asks    = book.get("levels", [[]])[1][:5]
    bid_vol = sum(float(b["sz"]) for b in bids)
    ask_vol = sum(float(a["sz"]) for a in asks)
    ok(f"Bid vol: {bid_vol:.4f} BTC | Ask vol: {ask_vol:.4f} BTC")
    ok(f"Ratio: {round(bid_vol/ask_vol,3) if ask_vol>0 else 'N/A'}")
except Exception as e:
    err(f"Errore order book: {e}")

# 8. OI + FUNDING
section("8. OPEN INTEREST + FUNDING RATE")
try:
    r4      = requests.post(f"{HL_URL}/info", json={"type": "metaAndAssetCtxs"}, timeout=15)
    btc_ctx = r4.json()[1][0]
    ok(f"Open Interest: {float(btc_ctx.get('openInterest',0)):,.2f} BTC")
    ok(f"Funding Rate: {float(btc_ctx.get('funding',0))*100:.4f}%")
except Exception as e:
    err(f"Errore OI/Funding: {e}")

# 9. FEAR & GREED
section("9. FEAR & GREED")
try:
    r5 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    d  = r5.json()["data"][0]
    ok(f"{d['value']} ({d['value_classification']})")
except Exception as e:
    err(f"Errore: {e}")

# 10. WHALE ALERT
section("10. WHALE ALERT")
try:
    r6   = requests.get("https://api.whale-alert.io/v1/transactions",
                        params={"api_key": "free", "min_value": 1000000,
                                "start": int(time.time())-3600, "limit": 3},
                        timeout=10)
    data = r6.json()
    txs  = data.get("transactions", [])
    ok(f"Transazioni: {len(txs)} | Status: {data.get('result','ok')}")
    for t in txs:
        ok(f"  {t.get('blockchain')}: {t.get('amount',0):,.0f} {t.get('symbol')} (${t.get('amount_usd',0):,.0f})")
except Exception as e:
    err(f"Errore: {e}")

# 11. NEWS API
section("11. NEWS API")
try:
    r7       = requests.get("https://newsapi.org/v2/everything",
                            params={"q": "bitcoin", "language": "en",
                                    "sortBy": "publishedAt", "pageSize": 3,
                                    "apiKey": NEWSAPI_KEY},
                            timeout=15)
    articles = r7.json().get("articles", [])
    ok(f"Articoli trovati: {len(articles)}")
    for a in articles[:3]:
        ok(f"  → {a['title'][:65]}...")
except Exception as e:
    err(f"Errore: {e}")

# 12. ANTHROPIC AI
section("12. ANTHROPIC AI")
try:
    r8   = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 50,
              "messages": [{"role": "user",
                            "content": 'Rispondi SOLO con: {"action":"HOLD","reason":"test ok"}'}]},
        timeout=30
    )
    resp = r8.json()
    if "content" in resp:
        ok(f"Risposta: {resp['content'][0]['text'].strip()}")
    else:
        err(f"Errore: {resp.get('error', resp)}")
except Exception as e:
    err(f"Errore: {e}")

section("RIEPILOGO")
print("  Debug completato! Controlla i ❌ sopra.")
print(f"  Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
