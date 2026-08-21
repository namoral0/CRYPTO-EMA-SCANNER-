import os
import ccxt
import pandas as pd
import requests
import json

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Zostawiamy główne kryptowaluty
SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP"]
TIMEFRAME = "4h"
CACHE_FILE = "cache.json"

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f: json.dump(cache, f)
    except Exception as e:
        print(f"Błąd zapisu cache: {e}")

def calculate_rsi(series, window=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/window, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/window, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def main():
    cache = load_cache()
    exchange = ccxt.kraken()
    
    for symbol in SYMBOLS:
        try:
            # Pobieranie danych z giełdy
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Wskaźniki
            df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
            df['RSI'] = calculate_rsi(df['close'])
            df['Vol_Avg'] = df['volume'].rolling(20).mean()
            
            ts = int(df['timestamp'].iloc[-2])
            last_price = df['close'].iloc[-2]
            rsi = df['RSI'].iloc[-2]
            vol_spike = df['volume'].iloc[-2] > (df['Vol_Avg'].iloc[-2] * 2) # Wolumen 2x wyższy
            
            # Warunek przecięcia
            is_crossover = (df['EMA_20'].iloc[-3] <= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2]) or \
                           (df['EMA_20'].iloc[-3] >= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] < df['EMA_50'].iloc[-2])
            
            if is_crossover and cache.get(symbol) != ts:
                direction = "🚀 W GÓRĘ" if df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2] else "🔻 W DÓŁ"
                spike_msg = "🔥 POTWIERDZENIE WOLUMENEM" if vol_spike else "Wolumen neutralny"
                
                msg = (f"{direction} **ALERT {symbol} ({TIMEFRAME})**\n\n"
                       f"💰 Cena: *£{last_price:.4f}*\n"
                       f"📊 RSI: {rsi:.1f}\n"
                       f"{spike_msg}\n\n"
                       f"EMA200: £{df['EMA_200'].iloc[-2]:.4f}")
                
                send_telegram_alert(msg)
                cache[symbol] = ts
                save_cache(cache)
                print(f"Wysłano alert dla {symbol}")
            else:
                print(f"{symbol}: Brak nowego sygnału.")
                
        except Exception as e:
            print(f"Błąd dla {symbol}: {e}")

if __name__ == "__main__":
    main()
