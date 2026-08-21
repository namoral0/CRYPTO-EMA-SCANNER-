import os
import ccxt
import pandas as pd
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "XRP/GBP"
TIMEFRAME = "4h"
CACHE_FILE = "last_candle.txt"

def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Brak tokenów Telegrama. Skonfiguruj sekrety w GitHub Actions.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Błąd wysyłania wiadomości na Telegram: {e}")

def get_last_alerted_timestamp():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return int(f.read().strip())
        except Exception:
            return 0
    return 0

def save_last_alerted_timestamp(timestamp: int):
    try:
        with open(CACHE_FILE, "w") as f:
            f.write(str(timestamp))
    except Exception as e:
        print(f"Błąd zapisu pliku cache: {e}")

def check_ema_crossover():
    print(f"Rozpoczynam skanowanie {SYMBOL} ({TIMEFRAME})...")
    
    try:
        exchange = ccxt.kraken()
        # 300 świec pozwala średniej EMA 200 prawidłowo się uformować
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    except Exception as e:
        print(f"Błąd pobierania danych z giełdy Kraken: {e}")
        return
    
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # Obliczanie średnich EMA
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()

    # Indeks -2 to ostatnia w pełni zamknięta świeca, -3 to świeca przed nią
    candle_timestamp = int(df['timestamp'].iloc[-2])
    
    prev_ema20 = df['EMA_20'].iloc[-3]
    prev_ema50 = df['EMA_50'].iloc[-3]
    curr_ema20 = df['EMA_20'].iloc[-2]
    curr_ema50 = df['EMA_50'].iloc[-2]
    curr_ema200 = df['EMA_200'].iloc[-2]
    last_price = df['close'].iloc[-2]

    last_alerted = get_last_alerted_timestamp()

    print(f"Ostatnia zamknięta cena: £{last_price:.4f} | EMA 20: £{curr_ema20:.4f} | EMA 50: £{curr_ema50:.4f}")

    # --- ZŁOTY KRZYŻ (Przecięcie EMA 50 od dołu) ---
    if prev_ema20 <= prev_ema50 and curr_ema20 > curr_ema50:
        if candle_timestamp == last_alerted:
            print("Sygnał zakupu dla tej świecy 4h został już wysłany. Pomijam.")
            return

        status = "✅ **Zgodny z trendem makro**" if last_price > curr_ema200 else "⚠️ **Przecięcie pod EMA 200 (wyższe ryzyko)**"
        msg = (
            f"🚀 **ALERT XRP ({TIMEFRAME})**: EMA 20 przebiła EMA 50 OD DOŁU!\n\n"
            f"💰 Cena: *£{last_price:.4f}*\n"
            f"📈 EMA 20: *£{curr_ema20:.4f}*\n"
            f"📉 EMA 50: *£{curr_ema50:.4f}*\n"
            f"🧱 EMA 200: *£{curr_ema200:.4f}*\n\n"
            f"Status: {status}"
        )
        send_telegram_alert(msg)
        save_last_alerted_timestamp(candle_timestamp)
        print("Wysłano alert o przecięciu w górę!")

    # --- KRZYŻ ŚMIERCI (Przecięcie EMA 50 od góry) ---
    elif prev_ema20 >= prev_ema50 and curr_ema20 < curr_ema50:
        if candle_timestamp == last_alerted:
            print("Sygnał sprzedaży dla tej świecy 4h został już wysłany. Pomijam.")
            return

        status = "✅ **Zgodny z trendem spadkowym**" if last_price < curr_ema200 else "⚠️ **Korekta w trendzie wzrostowym**"
        msg = (
            f"🔻 **ALERT XRP ({TIMEFRAME})**: EMA 20 przebiła EMA 50 OD GÓRY!\n\n"
            f"💰 Cena: *£{last_price:.4f}*\n"
            f"📈 EMA 20: *£{curr_ema20:.4f}*\n"
            f"📉 EMA 50: *£{curr_ema50:.4f}*\n"
            f"🧱 EMA 200: *£{curr_ema200:.4f}*\n\n"
            f"Status: {status}"
        )
        send_telegram_alert(msg)
        save_last_alerted_timestamp(candle_timestamp)
        print("Wysłano alert o przecięciu w dół!")
        
    else:
        print("Brak przecięcia średnich. Sytuacja stabilna.")

if __name__ == "__main__":
    check_ema_crossover()
