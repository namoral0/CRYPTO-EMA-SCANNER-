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

# Pobieranie zmiennych środowiskowych z Secrets (w GitHub Actions)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

# Oficjalne symbole Kraken Pro (w tym oznaczona giełdowo para fiat)
SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP", "LINK/GBP", "SOL/GBP", "ONDO/GBP:GBP"]
CACHE_FILE = "cache.json"

def send_telegram_alert(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Brak TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID w środowisku!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    print(f"Telegram status: {response.status_code}")

def add_to_tasks(title, notes):
    if not HAS_GOOGLE or not GOOGLE_TASKS_CREDENTIALS: 
        print("⚠️ Pomijam Google Tasks (brak bibliotek lub credentials).")
        return
    try:
        creds_dict = json.loads(GOOGLE_TASKS_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/tasks"])
        service = build('tasks', 'v1', credentials=creds)
        service.tasks().insert(tasklist='@default', body={'title': title, 'notes': notes}).execute()
        print("✅ Pomyślnie dodano do Google Tasks")
    except Exception as e:
        print(f"❌ Błąd Tasks: {e}")

def main():
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: 
                cache = json.load(f)
        except Exception: 
            pass
    
    # Włączamy bezpieczne opóźnienia dla Kraken Pro, by uniknąć blokady API
    exchange = ccxt.kraken({'enableRateLimit': True})
    
    for symbol in SYMBOLS:
        try:
            # 1. Pobieranie danych 1D (Trend główny)
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d
            
            # 2. Pobieranie danych 4H
            df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
            
            # Obliczenie RSI (14)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))
            
            ts_closed = int(df['ts'].iloc[-2])
            rsi_closed = round(df['RSI'].iloc[-2], 1)
            rsi_live = round(df['RSI'].iloc[-1], 1)
            last_price = df['close'].iloc[-1]
            
            # Przecięcia EMA
            crossover_up = (df['EMA_20'].iloc[-3] <= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] > df['EMA_50'].iloc[-2])
            crossover_down = (df['EMA_20'].iloc[-3] >= df['EMA_50'].iloc[-3] and df['EMA_20'].iloc[-2] < df['EMA_50'].iloc[-2])
            
            # Filtry sygnałów
            is_oversold = rsi_closed <= 30 or rsi_live <= 30
            is_overbought = rsi_closed >= 70 or rsi_live >= 70
            
            is_buy = is_oversold or (crossover_up and is_uptrend_1d)
            is_sell = is_overbought or crossover_down
            
            is_in_cache = cache.get(symbol) == ts_closed
            print(f"[{symbol}] Cena: £{last_price:.4f} | RSI: {rsi_live} | Buy: {is_buy} | Sell: {is_sell} | W Cache: {is_in_cache}")
            
            if (is_buy or is_sell) and not is_in_cache:
                if is_sell:
                    rodzaj = "**🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ! 🔴**"
                    task_title = f"PILNE: SPRZEDAJ {symbol} (Zagrożenie/RSI)"
                else:
                    rodzaj = "**🟢 OKAZJA ZAKUPOWA (RSI/EMA)**"
                    task_title = f"KUP {symbol} (Sygnał wejścia)"
                    
                msg = (
                    f"{rodzaj}\n\n"
                    f"🪙 **Moneta:** `{symbol}` (4H)\n"
                    f"💰 **Cena:** `£{last_price:.4f}`\n"
                    f"📊 **RSI (zamknięta świeca):** `{rsi_closed}`\n"
                    f"📊 **RSI (bieżąca świeca):** `{rsi_live}`\n"
                    f"📈 **Trend 1D:** `{'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}`"
                )
                
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg)
                cache[symbol] = ts_closed
                
        except Exception as e:
            print(f"❌ Błąd dla {symbol}: {e}")
            continue
    
    with open(CACHE_FILE, "w") as f: 
        json.dump(cache, f)

if __name__ == "__main__":
    main()
    
