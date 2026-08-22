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

# Zmienne środowiskowe z GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

# Pełna lista symboli z uwzględnieniem par USD przeliczanych na GBP
SYMBOLS = [
    "XRP/GBP", "BTC/GBP", "ETH/GBP", "LINK/GBP", "SOL/GBP", 
    "ONDO/USD", "SUI/GBP", "AAVE/GBP", "AVAX/USD", "NEAR/USD"
]
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
        # Wszystkie alerty z automatu przesyłamy bezpośrednio do Google Tasks
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
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    
    # Kurs przeliczeniowy USD -> GBP dla zachowania jednolitych kalkulacji
    usd_gbp_rate = 0.78
    try:
        ticker_fx = exchange.fetch_ticker("GBP/USD")
        if ticker_fx and ticker_fx.get('last'):
            usd_gbp_rate = 1.0 / ticker_fx['last']
    except Exception:
        pass

    for symbol in SYMBOLS:
        try:
            # 1. Pobieranie danych 1D (Trend główny)
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d
            
            # 2. Pobieranie danych 4H (Główne wskaźniki)
            df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            
            # Obliczenie RSI (14) dla 4H
            delta_4h = df_4h['close'].diff()
            gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_4h = gain_4h / loss_4h
            df_4h['RSI'] = 100 - (100 / (1 + rs_4h))
            
            # Wstęgi Bollingera (BB) dla 4H (okres 20, odchylenie std 2)
            df_4h['BB_mid'] = df_4h['close'].rolling(window=20).mean()
            df_4h['BB_std'] = df_4h['close'].rolling(window=20).std()
            df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
            df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)
            
            # 3. Pobieranie danych 15m (Do precyzyjnego potwierdzania sygnałów z 4H)
            df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="15m", limit=100), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            delta_15m = df_15m['close'].diff()
            gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_15m = gain_15m / loss_15m
            df_15m['RSI'] = 100 - (100 / (1 + rs_15m))

            # --- ZMIENNE BIEŻĄCE ---
            ts_closed = int(df_4h['ts'].iloc[-2])
            rsi_4h_live = round(df_4h['RSI'].iloc[-1], 1)
            rsi_15m_live = round(df_15m['RSI'].iloc[-1], 1)
            
            last_price = df_4h['close'].iloc[-1]
            bb_upper = df_4h['BB_upper'].iloc[-1]
            bb_lower = df_4h['BB_lower'].iloc[-1]
            
            # Wyceny prezentowane w GBP (£)
            if symbol.endswith("/USD"):
                price_gbp = last_price * usd_gbp_rate
                display_price = f"£{price_gbp:.4f} (${last_price:.4f})"
                display_symbol = symbol.replace("/USD", "/GBP")
            else:
                display_price = f"£{last_price:.4f}"
                display_symbol = symbol
            
            # --- LOGIKA DECYZYJNA ---
            crossover_up = (df_4h['EMA_20'].iloc[-3] <= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] > df_4h['EMA_50'].iloc[-2])
            crossover_down = (df_4h['EMA_20'].iloc[-3] >= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] < df_4h['EMA_50'].iloc[-2])
            
            # Nowe progi RSI (Ostrzeganie już od 65)
            is_oversold_4h = rsi_4h_live <= 30
            is_overbought_4h = rsi_4h_live >= 65
            
            # Wstęgi Bollingera - wyjście ceny poza kanał
            price_above_upper_bb = last_price >= bb_upper
            price_below_lower_bb = last_price <= bb_lower
            
            # 🟢 KUPNO: 
            # (Wyprzedanie 4H + potwierdzenie RSI 15m poniżej 40) LUB (Złoty krzyż w trendzie wzrostowym) LUB (Ekstremalne wyprzedanie poza dolną BB)
            is_buy = (is_oversold_4h and rsi_15m_live <= 40) or (crossover_up and is_uptrend_1d) or (is_oversold_4h and price_below_lower_bb)
            
            # 🔴 SPRZEDAŻ:
            # (Wykupienie 4H >= 65 + potwierdzenie RSI 15m powyżej 60) LUB (Krzyż śmierci) LUB (Wykupienie + przebicie górnej BB)
            is_sell = (is_overbought_4h and rsi_15m_live >= 60) or crossover_down or (is_overbought_4h and price_above_upper_bb)
            
            is_in_cache = cache.get(symbol) == ts_closed
            print(f"[{display_symbol}] Cena: {display_price} | RSI 4H: {rsi_4h_live} | RSI 15m: {rsi_15m_live} | Buy: {is_buy} | Sell: {is_sell} | Cache: {is_in_cache}")
            
            if (is_buy or is_sell) and not is_in_cache:
                if is_sell:
                    rodzaj = "**🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ! 🔴**"
                    task_title = f"PILNE: SPRZEDAJ {display_symbol} (Zagrożenie/RSI)"
                else:
                    rodzaj = "**🟢 OKAZJA ZAKUPOWA (RSI/EMA/BB)**"
                    task_title = f"KUP {display_symbol} (Sygnał wejścia)"
                    
                msg = (
                    f"{rodzaj}\n\n"
                    f"🪙 **Moneta:** `{display_symbol}` (4H/15m)\n"
                    f"💰 **Cena:** `{display_price}`\n"
                    f"📊 **RSI (4H):** `{rsi_4h_live}`\n"
                    f"⏱️ **RSI (15m):** `{rsi_15m_live}`\n"
                    f"📈 **Trend 1D:** `{'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}`\n"
                    f"🔥 **Status BB:** `{'Przebita górna wstęga' if price_above_upper_bb else 'Przebita dolna wstęga' if price_below_lower_bb else 'Wewnątrz kanału'}`"
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
            
