import asyncio
import datetime
import json
import logging
import os
import tempfile
import time
from zoneinfo import ZoneInfo

import ccxt
import gspread
import httpx
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- 1. KONFIGURACJA LOGOWANIA ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- 2. ZMIENNE ŚRODOWISKOWE I STAŁE ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GOOGLE_TASKS_CREDENTIALS = os.getenv('GOOGLE_TASKS_CREDENTIALS')
GOOGLE_TASK_LIST_ID = os.getenv('GOOGLE_TASK_LIST_ID') or '@default'

CORE_CRYPTO = ['BTC/GBP', 'ETH/GBP']
ALT_CRYPTO = [
    'SOL/GBP', 'TAO/USD', 'RENDER/USD', 
    'ONDO/USD', 'XRP/GBP', 'LINK/GBP', 
    'SUI/GBP', 'AAVE/GBP'
]
SYMBOLS = CORE_CRYPTO + ALT_CRYPTO
CACHE_FILE = 'cache_krypto.json'

GOOGLE_SEMAPHORE = asyncio.Semaphore(2)

exchange = ccxt.kraken({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})


# --- 3. INICJALIZACJA USŁUG I I/O ---
def init_google_services():
    if not GOOGLE_TASKS_CREDENTIALS:
        return None, None
    try:
        creds_dict = json.loads(GOOGLE_TASKS_CREDENTIALS)
        scopes = [
            "https://www.googleapis.com/auth/tasks",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        
        tasks_service = build('tasks', 'v1', credentials=creds, cache_discovery=False)
        sheet_client = gspread.authorize(creds)
        sheet = sheet_client.open_by_key(SPREADSHEET_ID).sheet1 if SPREADSHEET_ID else None
        
        return tasks_service, sheet
    except Exception as e:
        logging.error(f"Błąd inicjalizacji Google Services: {e}")
        return None, None

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {}

def save_cache(cache_data):
    """Zapis atomowy zapobiegający uszkodzeniu pliku JSON przy przerwaniu skryptu."""
    try:
        dir_name = os.path.dirname(os.path.abspath(CACHE_FILE)) or "."
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(cache_data, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, CACHE_FILE)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache: {e}")

async def send_telegram_alert_async(http_client: httpx.AsyncClient, msg: str, custom_chat_id=None):
    chat_id = custom_chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        response = await http_client.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Nie udało się wysłać wiadomości na Telegram: {e}")

async def add_to_tasks_async(tasks_service, title, notes):
    if not tasks_service:
        return
    async with GOOGLE_SEMAPHORE:
        try:
            await asyncio.to_thread(
                lambda: tasks_service.tasks().insert(
                    tasklist=GOOGLE_TASK_LIST_ID, 
                    body={'title': title, 'notes': notes}
                ).execute()
            )
            logging.info(f"Dodano zadanie do Google Tasks: {title}")
        except Exception as e:
            logging.error(f"Błąd Google Tasks: {e}")

def write_github_step_summary(digest_rows, health_msg=""):
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                if health_msg:
                    f.write(f"> {health_msg}\n\n")
                f.write("### 📊 Podsumowanie Skanera Krypto (Tryb Analityczny)\n\n")
                f.write("| Moneta | Cena | RSI 4H | Formacja | Stan |\n")
                f.write("| --- | --- | --- | --- | --- |\n")
                for r in digest_rows:
                    f.write(f"| **{r['symbol']}** | {r['price']} | {r['rsi']} | {r['pinbar']} | {r['status']} |\n")
        except Exception as e:
            logging.error(f"Błąd zapisu GITHUB_STEP_SUMMARY: {e}")

async def process_telegram_commands_async(http_client: httpx.AsyncClient, cache: dict, digest_rows: list):
    if not TELEGRAM_TOKEN:
        return

    last_offset = cache.get("telegram_update_offset", 0)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_offset + 1}&timeout=2"
    try:
        resp = await http_client.get(url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            for update in data.get("result", []):
                cache["telegram_update_offset"] = update["update_id"]
                msg_obj = update.get("message", {})
                text = msg_obj.get("text", "").strip().lower()
                chat_id = msg_obj.get("chat", {}).get("id")

                if text.startswith(("/status", "/stan", "/start")):
                    status_msg = "📋 **AKTUALNY STAN RYNKU (KRYPTO)**\n\n"
                    for r in digest_rows:
                        sym_disp = r['symbol']
                        tv_sym = r.get('tv_symbol', sym_disp.replace("/", ""))
                        tv_link = f"[{sym_disp}](https://www.tradingview.com/chart/?symbol=KRAKEN:{tv_sym})"
                        status_msg += f"{r['icon']} {tv_link}: {r['price']} | RSI: {r['rsi']} | {r['pinbar']} | {r['status']}\n"
                    status_msg += "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                    target_chat = chat_id or TELEGRAM_CHAT_ID
                    if target_chat:
                        await send_telegram_alert_async(http_client, status_msg, custom_chat_id=target_chat)
    except Exception as e:
        logging.error(f"Błąd przetwarzania komend Telegram: {e}")


# --- 4. ANALIZA TECHNICZNA I DANE ---
async def fetch_ohlcv_async(symbol, timeframe='4h', limit=100):
    try:
        ohlcv = await asyncio.to_thread(exchange.fetch_ohlcv, symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        return df
    except Exception as e:
        logging.warning(f"Błąd pobierania {symbol}: {e}")
        return None

async def get_gbp_rate_async():
    try:
        ticker = await asyncio.to_thread(exchange.fetch_ticker, 'USD/GBP')
        return float(ticker['last'])
    except Exception:
        return 0.79

def calculate_indicators(df, min_wick_ratio=0.65, max_upper_wick_ratio=0.20):
    # RSI 14 (Wygładzanie wykładnicze Wildera)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    
    rs = avg_gain / avg_loss
    df['rsi'] = round(100 - (100 / (1 + rs)), 2)
    
    # Wstęgi Bollingera (20, 2)
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std20'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['sma20'] + (df['std20'] * 2)
    df['bb_lower'] = df['sma20'] - (df['std20'] * 2)
    
    # Detekcja Pin Bara
    range_candle = df['high'] - df['low']
    body = abs(df['close'] - df['open'])
    
    lower_wick = df[['open', 'close']].min(axis=1) - df['low']
    upper_wick = df['high'] - df[['open', 'close']].max(axis=1)
    
    valid_range = range_candle > 0
    has_strong_lower_wick = (lower_wick / range_candle) >= min_wick_ratio
    has_small_upper_wick = (upper_wick / range_candle) <= max_upper_wick_ratio
    has_small_body = (body / range_candle) <= 0.35
    
    df['pinbar'] = valid_range & has_strong_lower_wick & has_small_upper_wick & has_small_body
    return df


# --- 5. GŁÓWNA PĘTLA APLIKACJI ---
async def main():
    start_time = time.time()
    logging.info("Uruchamianie Skanera Krypto (Pełny Tryb Analityczny)...")

    uk_now = datetime.datetime.now(ZoneInfo('Europe/London'))
    today_str = uk_now.strftime('%Y-%m-%d')
    now_str = uk_now.strftime('%Y-%m-%d %H:%M')

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    usd_to_gbp = await get_gbp_rate_async()

    digest_lines = []
    digest_summary_rows = []
    pending_alerts = []
    rows_to_append_sheets = []
    success_count = 0

    async with httpx.AsyncClient() as http_client:
        for symbol in SYMBOLS:
            try:
                is_core = symbol in CORE_CRYPTO
                crypto_icon = '🏛️' if is_core else '🚀'
                
                pinbar_threshold = 0.50 if is_core else 0.65
                rsi_signal_threshold = 35.0 if is_core else 30.0
                max_pinbar_rsi = 42.0 if is_core else 38.0
                
                df = await fetch_ohlcv_async(symbol)
                if df is None or len(df) < 25:
                    continue

                df = calculate_indicators(df, min_wick_ratio=pinbar_threshold, max_upper_wick_ratio=0.20)

                # Zamknięta świeca 4H (iloc[-2])
                last_row = df.iloc[-2]
                price_native = float(last_row['close'])
                rsi_4h = float(last_row['rsi'])
                is_pinbar = bool(last_row['pinbar'])
                ts_closed = int(last_row['timestamp'] / 1000)

                # Symbol do prawidłowego wykresu na TradingView (oryginalna para na Krakenie)
                tv_clean_symbol = symbol.replace("/", "")

                # Standaryzacja wyświetlania waluty do GBP pod Trading 212
                if 'USD' in symbol:
                    price_gbp = price_native * usd_to_gbp
                    symbol_display = symbol.replace('USD', 'GBP')
                else:
                    price_gbp = price_native
                    symbol_display = symbol

                price_disp = f"£{price_gbp:.4f}"

                # Kryteria wyzwolenia sygnału
                current_signal_type = 'NONE'
                if (rsi_4h <= rsi_signal_threshold) or (last_row['low'] <= last_row['bb_lower']):
                    current_signal_type = 'BUY_REBOUND'
                elif is_pinbar and rsi_4h <= max_pinbar_rsi:
                    current_signal_type = 'BUY_PINBAR'

                status_map = {
                    'NONE': '⚪ Neutralny',
                    'BUY_REBOUND': '⚡️ SZYBKIE ODBICIE',
                    'BUY_PINBAR': '🕯️ PINBAR ODBICIE'
                }
                status_txt = status_map.get(current_signal_type, '⚪ Neutralny')
                pinbar_txt = f"🕯️ Pinbar {int(pinbar_threshold * 100)}%" if is_pinbar else "Brak"

                tv_link_symbol = f"[{symbol_display}](https://www.tradingview.com/chart/?symbol=KRAKEN:{tv_clean_symbol})"

                digest_lines.append(f"{crypto_icon} {tv_link_symbol}: {price_disp} | RSI: {rsi_4h} | {pinbar_txt} | {status_txt}\n")
                digest_summary_rows.append({
                    'symbol': symbol_display, 'icon': crypto_icon,
                    'price': price_disp, 'rsi': rsi_4h, 'pinbar': pinbar_txt,
                    'status': status_txt, 'tv_symbol': tv_clean_symbol
                })

                # LOGIKA CACHE — ANTY-SPAM CO 15 MINUT
                ticker_cache = cache.get(symbol_display, {})
                last_sig = ticker_cache.get('signal', 'NONE')
                last_ts = ticker_cache.get('ts_closed', 0)
                last_alert_time = ticker_cache.get('last_alert_time', 0.0)
                last_sent_rsi = ticker_cache.get('last_sent_rsi', 0.0)

                current_epoch = time.time()
                rsi_changed = abs(rsi_4h - last_sent_rsi) >= 1.0

                should_alert = False
                if current_signal_type != 'NONE':
                    if last_ts != ts_closed or last_sig != current_signal_type:
                        should_alert = True
                    elif rsi_changed and (current_epoch - last_alert_time >= 1800):
                        should_alert = True

                signal_to_save = current_signal_type if current_signal_type != 'NONE' else (last_sig if last_ts == ts_closed else 'NONE')

                if should_alert:
                    tv_link = f"https://www.tradingview.com/chart/?symbol=KRAKEN:{tv_clean_symbol}"
                    t212_link = "https://live.trading212.com/"

                    pinbar_line = f"🕯 **Knot popytowy na świecy 4H (Pinbar >= {int(pinbar_threshold * 100)}%)**\n" if is_pinbar else ""

                    title = f"⚡️ ODBICIE KRYPTO: {symbol_display}"
                    body = (
                        f"⚡️ **SZYBKA OKAZJA NA ODBICIE (SWING)**\n\n"
                        f"{crypto_icon} **Moneta:** [{symbol_display}]({tv_link})\n"
                        f"💰 **Cena:** `{price_disp}`\n"
                        f"📊 **RSI 4H:** `{rsi_4h}`\n\n"
                        f"Potwierdzenia techniczne:\n"
                        f"{pinbar_line}"
                        f"🔗 [Zobacz wykres na TradingView]({tv_link})\n"
                        f"🔗 [Handluj na Trading 212]({t212_link})"
                    )

                    pending_alerts.append((title, body, symbol_display, status_txt, price_disp, rsi_4h))

                cache[symbol_display] = {
                    'signal': signal_to_save,
                    'date': today_str,
                    'ts_closed': ts_closed,
                    'last_alert_time': current_epoch if should_alert else last_alert_time,
                    'last_sent_rsi': rsi_4h if should_alert else last_sent_rsi
                }
                success_count += 1

            except Exception as e:
                logging.error(f"Błąd dla {symbol}: {e}")

        elapsed = round(time.time() - start_time, 2)
        health_summary = f"🩺 **Autodiagnostyka Krypto:** Przeanalizowano `{success_count}/{len(SYMBOLS)}` monet w `{elapsed}s`."
        logging.info(health_summary.replace('**', '').replace('`', ''))

        write_github_step_summary(digest_summary_rows, health_msg=health_summary)
        await process_telegram_commands_async(http_client, cache, digest_summary_rows)

        # Wysyłanie powiadomień i zapis zbiorczy
        if pending_alerts:
            alert_coroutines = []
            for task_title, msg, sym_disp, status_txt, price_disp, rsi_val in pending_alerts:
                alert_coroutines.append(send_telegram_alert_async(http_client, msg))
                alert_coroutines.append(add_to_tasks_async(tasks_service, task_title, msg.replace('**', '')))
                
                rows_to_append_sheets.append([
                    now_str,
                    sym_disp,
                    status_txt,
                    price_disp,
                    f"RSI: {rsi_val}"
                ])
            await asyncio.gather(*alert_coroutines)

        if sheet and rows_to_append_sheets:
            try:
                await asyncio.to_thread(sheet.append_rows, rows_to_append_sheets)
                logging.info(f"Zapisano {len(rows_to_append_sheets)} wierszy krypto w Google Sheets.")
            except Exception as e:
                logging.error(f"Nie udało się zapisać wierszy w Google Sheets: {e}")

        # CODZIENNE PODSUMOWANIE RYNKU KRYPTO O GODZINIE 21:00
        if uk_now.hour >= 21 and cache.get('DIGEST_DATE') != today_str:
            if digest_lines:
                digest_msg = '📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO - 21:00)**\n\n' + ''.join(digest_lines) + "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                await asyncio.gather(
                    send_telegram_alert_async(http_client, digest_msg),
                    add_to_tasks_async(tasks_service, f"Podsumowanie Krypto ({today_str})", digest_msg.replace('**', '').replace('`', ''))
                )
                cache['DIGEST_DATE'] = today_str

        save_cache(cache)

    logging.info(f"Skaner krypto zakończył działanie w czasie: {elapsed}s.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"🚨 KRYTYCZNY BŁĄD SKANERA KRYPTO: {e}", exc_info=True)
        err_msg = f'🚨 **🔴 KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`'
        
        async def send_critical_error():
            async with httpx.AsyncClient() as http_client:
                await send_telegram_alert_async(http_client, err_msg)
        
        asyncio.run(send_critical_error())
        raise e
