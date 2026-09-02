import ccxt
import pandas as pd
import numpy as np
import time
import logging
import requests

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Inicjalizacja giełdy Kraken przez CCXT z zabezpieczeniem rate limit
exchange = ccxt.kraken({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# Konfiguracja powiadomień oraz arkusza
TELEGRAM_BOT_TOKEN = "TWOJ_BOT_TOKEN"
TELEGRAM_CHAT_ID = "TWOJ_CHAT_ID"

SYMBOLS = [
    'BTC/GBP', 'ETH/GBP', 'SOL/GBP', 
    'TAO/USD', 'RENDER/USD', 'ONDO/USD', 
    'XRP/GBP', 'LINK/GBP', 'SUI/GBP', 'AAVE/GBP'
]

def fetch_with_backoff(symbol, timeframe='4h', limit=100, max_retries=5):
    retries = 0
    delay = 2
    while retries < max_retries:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            return df
        except Exception as e:
            retries += 1
            logging.warning(f"Błąd pobierania {symbol} (próba {retries}/{max_retries}): {e}")
            if retries >= max_retries:
                return None
            time.sleep(delay)
            delay *= 2
    return None

def calculate_indicators(df):
    # RSI 14
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # Wstęgi Bollingera (20, 2)
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['sma20'] + (df['std20'] * 2)
    df['bb_lower'] = df['sma20'] - (df['std20'] * 2)
    df['bbw'] = (df['bb_upper'] - df['bb_lower']) / df['sma20']
    
    # Detekcja Pinbara (knot popytowy)
    body = abs(df['close'] - df['open'])
    range_candle = df['high'] - df['low']
    lower_wick = df['low'] - pd.concat([df['open'], df['close']], axis=1).min(axis=1)
    df['pinbar'] = (lower_wick > (range_candle * 0.6)) & (body < (range_candle * 0.3))
    
    return df

def get_gbp_rate():
    try:
        ticker = exchange.fetch_ticker('USD/GBP')
        return ticker['last']
    except Exception:
        return 0.79

def send_telegram_alert(symbol, price, rsi, is_pinbar, trend_text):
    clean_symbol = symbol.replace("/", "")
    tv_link = f"https://www.tradingview.com/chart/?symbol=KRAKEN:{clean_symbol}"
    
    pinbar_line = "🕯 Knot popytowy na świecy 4H (Pinbar)\n" if is_pinbar else ""
    
    message = (
        f"⚡️ *SZYBKA OKAZJA NA ODBICIE (SWING)*\n\n"
        f"🪙 Moneta: [{symbol}]({tv_link}) | Cena: £{price:.4f}\n"
        f"📊 RSI 4H: {rsi:.1f}\n"
        f"📈 Trend 1D: {trend_text} 🟡\n\n"
        f"Potwierdzenia techniczne:\n"
        f"{pinbar_line}"
        f"🌐 [Zobacz wykres na TradingView]({tv_link})\n"
        f"💼 [Handluj na Trading 212](https://www.trading212.com/)"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
        'disable_web_page_preview': True
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Błąd wysyłania alertu Telegram: {e}")

def main():
    usd_to_gbp = get_gbp_rate()
    for symbol in SYMBOLS:
        df = fetch_with_backoff(symbol)
        if df is None or len(df) < 25:
            continue
        df = calculate_indicators(df)
        
        # Analiza zamkniętej świecy (iloc[-2]) dla eliminacji fałszywych alarmów w trakcie tworzenia świecy
        last_row = df.iloc[-2]
        price = last_row['close']
        rsi = last_row['rsi']
        is_pinbar = last_row['pinbar']
        
        # Przeliczenie USD na GBP pod standard Trading 212
        if 'USD' in symbol:
            price = price * usd_to_gbp
            symbol_display = symbol.replace('USD', 'GBP')
        else:
            symbol_display = symbol

        # Warunki wyzwalania alertu
        if rsi < 35 or last_row['low'] <= last_row['bb_lower']:
            send_telegram_alert(symbol_display, price, rsi, is_pinbar, "Spadkowy/Korekta")
        
        time.sleep(1)

if __name__ == "__main__":
    main()
