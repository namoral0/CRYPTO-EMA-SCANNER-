import os
import ccxt
import pandas as pd
import requests
import json
import time

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

# Pełna, usystematyzowana lista monitorowanych aktywów
SYMBOLS = [
    "XRP/GBP", "BTC/GBP", "ETH/GBP", "LINK/GBP", "SOL/GBP", 
    "ONDO/USD", "SUI/GBP", "AAVE/GBP", "AVAX/USD", "NEAR/USD",
    "TAO/USD", "RENDER/USD"
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
    
    # Automatyczne pobranie kursu GBP/USD do przeliczeń cenowych
    usd_gbp_rate = 0.78
    try:
        ticker_fx = exchange.fetch_ticker("GBP/USD")
        if ticker_fx and ticker_fx.get('last'):
            usd_gbp_rate = 1.0 / ticker_fx['last']
    except Exception:
        pass

    for symbol in SYMBOLS:
        try:
            # Odstęp czasowy chroniący przed przeciążeniem API giełdy (Rate Limit)
            time.sleep(1)

            # 1. Pobieranie danych 1D (Trend główny - filtr makro)
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d
            
            # 2. Pobieranie danych 4H (Struktura i główny sygnał)
            df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            
            # RSI (14) dla 4H
            delta_4h = df_4h['close'].diff()
            gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_4h = gain_4h / loss_4h
            df_4h['RSI'] = 100 - (100 / (1 + rs_4h))
            
            # Wstęgi Bollingera (BB) dla 4H
            df_4h['BB_mid'] = df_4h['close'].rolling(window=20).mean()
            df_4h['BB_std'] = df_4h['close'].rolling(window=20).std()
            df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
            df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)

            # ATR (Average True Range) do wykrywania anomalii cenowych / paniki
            high_low = df_4h['h'] - df_4h['l']
            high_close = (df_4h['h'] - df_4h['close'].shift()).abs()
            low_close = (df_4h['l'] - df_4h['close'].shift()).abs()
            true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_4h['ATR'] = true_range.rolling(window=14).mean()
            df_4h['ATR_MA'] = df_4h['ATR'].rolling(window=20).mean()
            
            # 3. Pobieranie danych 15m (Timing wejścia)
            df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="15m", limit=100), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            delta_15m = df_15m['close'].diff()
            gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_15m = gain_15m / loss_15m
            df_15m['RSI'] = 100 - (100 / (1 + rs_15m))

            # Zmienne obliczeniowe dla bieżącej świecy
            ts_closed = int(df_4h['ts'].iloc[-2])
            rsi_4h_live = round(df_4h['RSI'].iloc[-1], 1)
            rsi_15m_live = round(df_15m['RSI'].iloc[-1], 1)
            
            last_price = df_4h['close'].iloc[-1]
            bb_upper = df_4h['BB_upper'].iloc[-1]
            bb_lower = df_4h['BB_lower'].iloc[-1]
            current_atr = df_4h['ATR'].iloc[-1]
            avg_atr = df_4h['ATR_MA'].iloc[-1]

            # Sprawdzenie, czy świeca nie jest anomalia (ochrona przed pułapką płynności)
            is_anomaly_candle = current_atr > (avg_atr * 2.5) if not pd.isna(avg_atr) else False
            
            # Formatowanie wyświetlanej ceny w walucie bazowej (£ / $)
            if symbol.endswith("/USD"):
                price_gbp = last_price * usd_gbp_rate
                display_price = f"£{price_gbp:.4f} (${last_price:.4f})"
                display_symbol = symbol.replace("/USD", "/GBP")
            else:
                display_price = f"£{last_price:.4f}"
                display_symbol = symbol
            
            # Indywidualne progi dopasowane do klasy aktywa (Blue-chip vs Altcoin)
            if "BTC" in symbol or "ETH" in symbol:
                buy_rsi_threshold = 30
                sell_rsi_threshold = 70
                confirm_15m_buy = 40
                confirm_15m_sell = 60
                profile_type = "Blue-chip"
            else:
                buy_rsi_threshold = 25
                sell_rsi_threshold = 75
                confirm_15m_buy = 35
                confirm_15m_sell = 65
                profile_type = "Altcoin"

            # Logika przecięć EMA
            crossover_up = (df_4h['EMA_20'].iloc[-3] <= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] > df_4h['EMA_50'].iloc[-2])
            crossover_down = (df_4h['EMA_20'].iloc[-3] >= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] < df_4h['EMA_50'].iloc[-2])
            
            is_oversold_4h = rsi_4h_live <= buy_rsi_threshold
            is_overbought_4h = rsi_4h_live >= sell_rsi_threshold
            
            price_above_upper_bb = last_price >= bb_upper
            price_below_lower_bb = last_price <= bb_lower
            
            # Ostateczne warunki sygnałów z uwzględnieniem filtra ATR
            is_buy = ((is_oversold_4h and rsi_15m_live <= confirm_15m_buy) or (crossover_up and is_uptrend_1d) or (is_oversold_4h and price_below_lower_bb)) and not is_anomaly_candle
            is_sell = ((is_overbought_4h and rsi_15m_live >= confirm_15m_sell) or crossover_down or (is_overbought_4h and price_above_upper_bb)) and not is_anomaly_candle

            current_signal_type = "NONE"
            if is_buy:
                current_signal_type = "BUY"
            elif is_sell:
                current_signal_type = "SELL"

            # Inteligentny cache stanowy (reaguje na zmianę stanu sygnału lub nową świecę)
            cached_data = cache.get(symbol, {})
            cached_ts = cached_data.get("ts")
            cached_signal = cached_data.get("signal")

            should_alert = current_signal_type != "NONE" and (cached_ts != ts_closed or cached_signal != current_signal_type)

            print(f"[{display_symbol}] Cena: {display_price} | RSI 4H: {rsi_4h_live} | Stan: {current_signal_type} | Anomalia ATR: {is_anomaly_candle} | Alert: {should_alert}")
            
            if should_alert:
                if current_signal_type == "SELL":
                    rodzaj = "**🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ! 🔴**"
                    task_title = f"PILNE: SPRZEDAJ {display_symbol} (Zagrożenie/RSI {sell_rsi_threshold})"
                else:
                    rodzaj = "**🟢 OKAZJA ZAKUPOWA (RSI/EMA/BB)**"
                    task_title = f"KUP {display_symbol} (Sygnał wejścia RSI {buy_rsi_threshold})"
                    
                msg = (
                    f"{rodzaj}\n\n"
                    f"🪙 **Moneta:** `{display_symbol}` ({profile_type})\n"
                    f"💰 **Cena:** `{display_price}`\n"
                    f"📊 **RSI 4H / Próg:** `{rsi_4h_live} / {sell_rsi_threshold if current_signal_type=='SELL' else buy_rsi_threshold}`\n"
                    f"⏱️ **RSI (15m):** `{rsi_15m_live}`\n"
                    f"📈 **Trend 1D:** `{'Wzrostowy' if is_uptrend_1d else 'Spadkowy'}`\n"
                    f"🛡️ **Filtr ATR:** `Stabilnie (brak anomalii)`"
                )
                
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg)
                
                # Zapis nowego stanu w cache
                cache[symbol] = {
                    "ts": ts_closed,
                    "signal": current_signal_type
                }
                
        except Exception as e:
            print(f"❌ Błąd dla {symbol}: {e}")
            continue
    
    with open(CACHE_FILE, "w") as f: 
        json.dump(cache, f)

if __name__ == "__main__":
    main()
                
