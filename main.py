import os
import ccxt
import pandas as pd
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "XRP/GBP"
TIMEFRAME = "4h"

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak tokenów Telegrama.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Błąd wysyłania: {e}")

def check_ema_crossover():
    exchange = ccxt.kraken()
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # Natywne obliczanie EMA za pomocą pandas (.ewm)
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()

    prev_ema20 = df['EMA_20'].iloc[-3]
    prev_ema50 = df['EMA_50'].iloc[-3]
    curr_ema20 = df['EMA_20'].iloc[-2]
    curr_ema50 = df['EMA_50'].iloc[-2]
    curr_ema200 = df['EMA_200'].iloc[-2]
    last_price = df['close'].iloc[-2]

    if prev_ema20 <= prev_ema50 and curr_ema20 > curr_ema50:
        status = "✅ **Zgodny z trendem makro**" if last_price > curr_ema200 else "⚠️ **Przecięcie pod EMA 200 (wyższe ryzyko)**"
        msg = f"🚀 **ALERT XRP ({TIMEFRAME})**: EMA 20 przebiła EMA 50 OD DOŁU!\n\nCena: £{last_price:.4f}\nEMA 200: £{curr_ema200:.4f}\nStatus: {status}"
        send_telegram_alert(msg)

    elif prev_ema20 >= prev_ema50 and curr_ema20 < curr_ema50:
        status = "✅ **Zgodny z trendem spadkowym**" if last_price < curr_ema200 else "⚠️ **Korekta w trendzie wzrostowym**"
        msg = f"🔻 **ALERT XRP ({TIMEFRAME})**: EMA 20 przebiła EMA 50 OD GÓRY!\n\nCena: £{last_price:.4f}\nEMA 200: £{curr_ema200:.4f}\nStatus: {status}"
        send_telegram_alert(msg)
    else:
        print(f"Brak nowego sygnału. Ostatnia cena: £{last_price:.4f}")

if __name__ == "__main__":
    check_ema_crossover()
