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

# Pobieranie zmiennych środowiskowych
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

# Pełna lista aktywów w GBP
SYMBOLS = ["XRP/GBP", "BTC/GBP", "ETH/GBP", "LINK/GBP", "SOL/GBP", "ONDO/GBP", "SUI/GBP", "AAVE/GBP", "AVAX/GBP", "NEAR/GBP"]
CACHE_FILE = "cache.json"

# Indywidualne profile progów RSI
CUSTOM_SETTINGS = {
    "BTC/GBP": {"rsi_buy": 30, "rsi_sell": 70},
    "ETH/GBP": {"rsi_buy": 30, "rsi_sell": 70},
    "XRP/GBP": {"rsi_buy": 30, "rsi_sell": 70},
    # Altcoiny otrzymują szersze marginesy tolerancji (25/75)
    "default": {"rsi_buy": 25, "rsi_sell": 75}
}

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

def get_rsi(df, periods=14):
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).ewm(alpha=1/periods, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/periods, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def main():
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: 
                cache = json.load(f)
        except Exception: 
            pass
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    
    for symbol in SYMBOLS:
        try:
            # Ustalanie progów dla konkretnej monety
            settings = CUSTOM_SETTINGS.get(symbol, CUSTOM_SETTINGS["default"])
            buy_threshold = settings["rsi_buy"]
            sell_threshold = settings["rsi_sell"]

            # 1. Pobieranie danych 1D (Trend główny)
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d
            
            # 2. Pobieranie danych 4H
            df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            df_4h['RSI'] = get_rsi(df_4h)
            
            # 3. Pobieranie danych 15m dla dodatkowego kontekstu
            df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="15m", limit=100), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df_15m['RSI'] = get_rsi(df_15m)
            rsi_15m_live = round(df_15m['RSI'].iloc[-1], 1)

            ts_closed = int(df_4h['ts'].iloc[-2])
            rsi_closed = round(df_4h['RSI'].iloc[-2], 1)
            rsi_live = round(df_4h['RSI'].iloc[-1], 1)
            last_price = df_4h['close'].iloc[-1]
            
            # Przecięcia EMA na interwale 4H
            crossover_up = (df_4h['EMA_20'].iloc[-3] <= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] > df_4h['EMA_50'].iloc[-2])
            crossover_down = (df_4h['EMA_20'].iloc[-3] >= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] < df_4h['EMA_50'].iloc[-2])
            
            # Weryfikacja sygnałów zgodnie z indywidualnym progiem
            is_oversold = rsi_closed <= buy_threshold or rsi_live <= buy_threshold
            is_overbought = rsi_closed >= sell_threshold or rsi_live >= sell_threshold
            
            is_buy = is_oversold or (crossover_up and is_uptrend_1d)
            is_sell = is_overbought or crossover_down
            
            is_in_cache = cache.get(symbol) == ts_closed
            
            print(f"[{symbol}] Cena: £{last_price:.4f} | RSI 4H: {rsi_live} | RSI 15m: {rsi_15m_live} | Buy: {is_buy} | Sell: {is_sell} | Cache: {is_in_cache}")
            
            # Egzekucja alertów (integracja z zadaniami)
            if (is_buy or is_sell) and not is_in_cache:
                if is_sell:
                    # Krytyczna komunikacja
                    rodzaj = "🚨 **🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ! 🔴** 🚨"
                    task_title = f"🔴 PILNA OPERACJA: SPRZEDAJ {symbol} (ZAGROŻENIE/RSI) 🔴"
                else:
                    rodzaj = "**🟢 OKAZJA ZAKUPOWA (RSI/EMA)**"
                    task_title = f"WYKONAJ ZAKUP: {symbol} (Sygnał wejścia)"
                    
                msg = (
                    f"{rodzaj}\n\n"
                    f"🪙 **Moneta:** `{symbol}`\n"
                    f"💰 **Cena:** `£{last_price:.4f}`\n"
                    f"📊 **RSI 4H (bieżące):** `{rsi_live}`\n"
                    f"⏱️ **RSI 15m (bieżące):** `{rsi_15m_live}`\n"
                    f"🎯 **Wymagany próg RSI:** `{buy_threshold} (Kupno) / {sell_threshold} (Sprzedaż)`\n"
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
    
