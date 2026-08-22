import os
import ccxt
import pandas as pd
import requests
import json

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP"]
CACHE_FILE = "cache.json"

def send_telegram_alert(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def add_to_tasks(title, notes):
    if not HAS_GOOGLE or not GOOGLE_TASKS_CREDENTIALS: return
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
            
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))
            
            # Wartości dla ostatniej zamkniętej świecy (-2) oraz bieżącej (-1)
            ts_closed = int(df['ts'].iloc[-2])
            rsi_closed = round(df['RSI'].iloc[-2], 1)
            rsi_live = round(df['RSI'].iloc[-1], 1)
            last_price = df['close'].iloc[-1]
            
            crossover_up = (df['EMA_20'].iloc[-3] <= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2])
            crossover_down = (df['EMA_20'].iloc[-3] >= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] < df['EMA_50'].iloc[-2])
            
            if crossover_up and not is_uptrend_1d: crossover_up = False
            
            # Reagujemy na wykupienie zarówno na zamkniętej świecy, jak i bieżącej (live)
            is_buy = crossover_up
            is_sell = crossover_down or rsi_closed >= 65 or rsi_live >= 65
            
            # Raport w konsoli GitHuba dla pełnej przejrzystości
            print(f"[{symbol}] Cena: £{last_price:.4f} | RSI 4h (zamknięta): {rsi_closed} | RSI 4h (live): {rsi_live} | Trend 1D: {'Wzrost' if is_uptrend_1d else 'Spadek'} | Sygnał: {is_buy or is_sell}")
            
            if (is_buy or is_sell) and cache.get(symbol) != ts_closed:
                if is_sell:
                    rodzaj = "🔴 **KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ!** 🔴"
                    task_title = f"PILNE: SPRZEDAJ? {symbol} (Zagrożenie/RSI)"
                else:
                    rodzaj = "🟢 OKAZJA ZAKUPOWA"
                    task_title = f"KUP {symbol} (Sygnał EMA)"
                    
                msg = (
                    f"🚨 {rodzaj}\n"
                    f"Moneta: {symbol} (4h)\n"
                    f"💰 Cena: £{last_price:.4f}\n"
                    f"📊 RSI (zamknięta): {rsi_closed}\n"
                    f"📊 RSI (bieżąca): {rsi_live}\n"
                    f"📈 Trend 1D: {'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}"
                )
                
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg.replace('**', ''))
                cache[symbol] = ts_closed
                
        except Exception as e:
            print(f"Błąd dla {symbol}: {e}")
            continue
    
    with open(CACHE_FILE, "w") as f: json.dump(cache, f)

if __name__ == "__main__":
    main()
            
