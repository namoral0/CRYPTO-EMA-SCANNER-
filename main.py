import os
import ccxt
import pandas as pd
import requests
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP"]
CACHE_FILE = "cache.json"

def send_telegram_alert(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def add_to_tasks(title, notes):
    if not GOOGLE_TASKS_CREDENTIALS: return
    try:
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_TASKS_CREDENTIALS), scopes=["https://www.googleapis.com/auth/tasks"])
        build('tasks', 'v1', credentials=creds).tasks().insert(tasklist='@default', body={'title': title, 'notes': notes}).execute()
    except Exception as e:
        print(f"Błąd Tasks: {e}")

def main():
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: cache = json.load(f)
        except: pass
    
    exchange = ccxt.kraken()
    for symbol in SYMBOLS:
        try:
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            
            df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
            df['RSI'] = 100 - (100 / (1 + (df['close'].diff().clip(lower=0).ewm(alpha=1/14).mean() / (-df['close'].diff().clip(upper=0).ewm(alpha=1/14).mean()))))
            
            ts = int(df['ts'].iloc[-2])
            rsi = df['RSI'].iloc[-2]
            last_price = df['close'].iloc[-2]
            
            crossover_up = (df['EMA_20'].iloc[-3] <= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2])
            crossover_down = (df['EMA_20'].iloc[-3] >= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] < df['EMA_50'].iloc[-2])
            
            if crossover_up and not is_uptrend_1d: crossover_up = False
            
            is_buy = crossover_up
            is_sell = crossover_down or rsi >= 65
            
            if (is_buy or is_sell) and cache.get(symbol) != ts:
                if is_sell:
                    rodzaj = "🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ"
                    task_title = f"SPRZEDAJ? {symbol} (Zagrożenie/RSI)"
                else:
                    rodzaj = "🟢 OKAZJA ZAKUPOWA"
                    task_title = f"KUP {symbol} (Sygnał EMA)"
                    
                msg = (
                    f"🚨 {rodzaj}\n"
                    f"Moneta: {symbol} (4h)\n"
                    f"💰 Cena: £{last_price:.4f}\n"
                    f"📊 RSI: {rsi:.1f}\n"
                    f"📈 Trend 1D: {'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}"
                )
                
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg)
                cache[symbol] = ts
                
        except Exception as e:
            print(f"Błąd dla {symbol}: {e}")
            continue
    
    with open(CACHE_FILE, "w") as f: json.dump(cache, f)

if __name__ == "__main__":
    main()
