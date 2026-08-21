import os
import ccxt
import pandas as pd
import requests
import json

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP"]
CACHE_FILE = "cache.json"

def send_telegram_alert(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def main():
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f: cache = json.load(f)
    
    exchange = ccxt.kraken()
    for symbol in SYMBOLS:
        try:
            # Filtr 1D
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            
            # Dane 4h
            df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['RSI'] = 100 - (100 / (1 + (df['close'].diff().clip(lower=0).ewm(alpha=1/14).mean() / (-df['close'].diff().clip(upper=0).ewm(alpha=1/14).mean()))))
            
            ts = int(df['ts'].iloc[-2])
            last_price = df['close'].iloc[-2]
            
            crossover_up = (df['EMA_20'].iloc[-3] <= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2])
            if crossover_up and not is_uptrend_1d: crossover_up = False
            
            if crossover_up and cache.get(symbol) != ts:
                msg = (f"🚀 **SYGNAŁ EMA {symbol} (4h)**\n\n"
                       f"💰 Cena: *£{last_price:.4f}*\n"
                       f"📈 Trend 1D: {'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}\n"
                       f"✅ EMA 20 przebiła EMA 50!")
                send_telegram_alert(msg)
                cache[symbol] = ts
        except: continue
    
    with open(CACHE_FILE, "w") as f: json.dump(cache, f)

if __name__ == "__main__":
    main()
