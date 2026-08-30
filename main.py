import asyncio
import datetime
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt.async_support as ccxt
import gspread
import httpx
import pandas as pd

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

# --- 1. KONFIGURACJA LOGOWANIA ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- 2. ZMIENNE ŚRODOWISKOWE I STAŁE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

GOOGLE_TASK_LIST_ID = os.getenv("GOOGLE_TASK_LIST_ID") or "@default"
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1XoG-AYYK06BNDmRYrtBvR2MdLKIcH638JnjYKnuV3pk")

FIXED_RISK_GBP = 10.0

CORE_CRYPTO = ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
SYMBOLS = [
    "TAO/USD", "BTC/GBP", "ETH/GBP", "SOL/GBP", 
    "XRP/GBP", "RENDER/USD", "SUI/GBP", "LINK/GBP", "AAVE/GBP"
]
CACHE_FILE = "cache_krypto.json"

# Ochrona przed limitami zapytań Krakena oraz Google API
KRAKEN_SEMAPHORE = asyncio.Semaphore(3)
GOOGLE_SEMAPHORE = asyncio.Semaphore(2)

# Czas blokady powtórnego powiadomienia (Cooldown: 24 godziny)
SIGNAL_COOLDOWN_SECONDS = 86400


# --- 3. INICJALIZACJA USŁUG I I/O ---
def init_google_services():
    """Jednorazowa autoryzacja Google Tasks i Google Sheets."""
    if not HAS_GOOGLE or not GOOGLE_TASKS_CREDENTIALS:
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
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if "active_positions" not in data:
                    data["active_positions"] = {}
                return data
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {"active_positions": {}}

def save_cache(cache_data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache: {e}")

async def send_telegram_alert_async(http_client: httpx.AsyncClient, msg: str, custom_chat_id=None):
    """Natywnie asynchroniczne wysyłanie wiadomości na Telegram z użyciem Connection Pooling."""
    chat_id = custom_chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        await http_client.post(url, json=payload, timeout=10.0)
    except Exception as e:
        logging.error(f"Nie udało się wysłać wiadomości na Telegram: {e}")

async def add_to_tasks_async(tasks_service, title, notes):
    """Nieblokujące dodawanie zadania do Google Tasks z zabezpieczeniem semaforem."""
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
    """Generuje tabelę Markdown bezpośrednio w podsumowaniu GitHub Actions wraz z diagnostyką."""
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                if health_msg:
                    f.write(f"> {health_msg}\n\n")
                f.write("### 📊 Podsumowanie Skanera Krypto\n\n")
                f.write("| Moneta | Cena | RSI 4H | ADX 4H | Trend 1D | Stan |\n")
                f.write("| --- | --- | --- | --- | --- | --- |\n")
                for r in digest_rows:
                    f.write(f"| **{r['symbol']}** | {r['price']} | {r['rsi']} | {r['adx']} | {r['trend']} | {r['status']} |\n")
        except Exception as e:
            logging.error(f"Błąd zapisu GITHUB_STEP_SUMMARY: {e}")

async def process_telegram_commands_async(http_client: httpx.AsyncClient, cache: dict, digest_rows: list):
    """Obsługa interaktywnych komend (/status, /stan) na Telegramie."""
    if not TELEGRAM_TOKEN:
        return
    
    try:
        del_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=false"
        await http_client.get(del_url, timeout=5.0)
    except Exception:
        pass

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
                        sym_link = f"[{r['symbol']}](https://live.trading212.com/)"
                        div = r.get('div_tag', '')
                        status_msg += f"🪙 {sym_link}: {r['price']} | RSI: {r['rsi']} | {r['status']}{div}\n"
                    
                    status_msg += "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                    
                    target_chat = chat_id or TELEGRAM_CHAT_ID
                    if target_chat:
                        await send_telegram_alert_async(http_client, status_msg, custom_chat_id=target_chat)
                        logging.info(f"Odpowiedziano na komendę {text} dla chat_id: {target_chat}")
    except Exception as e:
        logging.error(f"Błąd przetwarzania komend Telegram: {e}")


# --- 4. FUNKCJE ANALITYCZNE I MATEMATYCZNE ---
def check_bullish_divergence(df_4h, lookback=35):
    """Wykrywa byczą dywergencję RSI na podstawie punktów zwrotnych (Pivot Lows)."""
    try:
        if len(df_4h) < lookback + 5:
            return False

        sub_df = df_4h.iloc[-(lookback + 1):-1].copy().reset_index(drop=True)
        n = len(sub_df)
        
        pivot_lows = []
        for i in range(2, n - 2):
            is_pivot = (
                sub_df['l'].iloc[i] <= sub_df['l'].iloc[i-1] and 
                sub_df['l'].iloc[i] <= sub_df['l'].iloc[i-2] and 
                sub_df['l'].iloc[i] <= sub_df['l'].iloc[i+1] and 
                sub_df['l'].iloc[i] <= sub_df['l'].iloc[i+2]
            )
            if is_pivot:
                pivot_lows.append((i, sub_df['l'].iloc[i], sub_df['RSI'].iloc[i]))

        if len(pivot_lows) < 2:
            return False

        prev_pivot = pivot_lows[-2]
        curr_pivot = pivot_lows[-1]

        price_lower = curr_pivot[1] < prev_pivot[1]
        rsi_higher = curr_pivot[2] > prev_pivot[2]
        rsi_oversold = curr_pivot[2] < 45
        valid_distance = 3 <= (curr_pivot[0] - prev_pivot[0]) <= 25

        if price_lower and rsi_higher and rsi_oversold and valid_distance:
            return True

    except Exception:
        pass
    return False

def calculate_position_size(price_gbp, sl_gbp, fixed_risk=FIXED_RISK_GBP):
    """Oblicza wielkość pozycji w GBP dla ryzyka £10."""
    try:
        if price_gbp <= sl_gbp or sl_gbp <= 0:
            return 0.0
        risk_pct = (price_gbp - sl_gbp) / price_gbp
        if risk_pct <= 0:
            return 0.0
        position_size_gbp = fixed_risk / risk_pct
        return round(position_size_gbp, 2)
    except Exception:
        return 0.0

def compute_indicators(df_1d, df_4h, df_15m):
    """Wyliczanie wskaźników technicznych."""
    df_1d['EMA_200'] = df_1d['close'].ewm(span=200, adjust=False).mean()

    df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
    df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    
    delta_4h = df_4h['close'].diff()
    gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_4h['RSI'] = 100 - (100 / (1 + (gain_4h / loss_4h)))
    
    df_4h['BB_mid'] = df_4h['close'].rolling(20).mean()
    df_4h['BB_std'] = df_4h['close'].rolling(20).std()
    df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
    df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)
    df_4h['BBW'] = (df_4h['BB_upper'] - df_4h['BB_lower']) / df_4h['BB_mid']
    df_4h['BBW_MA'] = df_4h['BBW'].rolling(20).mean()

    true_range = pd.concat([
        df_4h['h'] - df_4h['l'], 
        (df_4h['h'] - df_4h['close'].shift()).abs(), 
        (df_4h['l'] - df_4h['close'].shift()).abs()
    ], axis=1).max(axis=1)
    
    df_4h['ATR'] = true_range.rolling(14).mean()
    df_4h['ATR_MA'] = df_4h['ATR'].rolling(20).mean()
    df_4h['Vol_MA'] = df_4h['v'].rolling(20).mean()

    plus_dm = df_4h['h'].diff()
    minus_dm = df_4h['l'].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr_14_ewm = true_range.ewm(alpha=1/14, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_14_ewm.replace(0, 1e-10))
    minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_14_ewm.replace(0, 1e-10))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-10)
    df_4h['ADX'] = dx.ewm(alpha=1/14, adjust=False).mean()

    delta_15m = df_15m['close'].diff()
    gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_15m['RSI'] = 100 - (100 / (1 + (gain_15m / loss_15m)))

    return df_1d, df_4h, df_15m


# --- 5. ASYNCHRONICZNE POBIERANIE DANYCH ---
async def fetch_ohlcv_retry_async(exchange, symbol, timeframe, limit=250, retries=3, delay=1.0):
    async with KRAKEN_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep(delay)

async def fetch_ticker_safe(exchange, symbol):
    async with KRAKEN_SEMAPHORE:
        return await exchange.fetch_ticker(symbol)

async def fetch_symbol_data(exchange, symbol):
    try:
        res_1d, res_4h, res_15m = await asyncio.gather(
            fetch_ohlcv_retry_async(exchange, symbol, "1d", 250),
            fetch_ohlcv_retry_async(exchange, symbol, "4h", 300),
            fetch_ohlcv_retry_async(exchange, symbol, "15m", 100)
        )
        cols = ['ts', 'o', 'h', 'l', 'close', 'v']
        return symbol, pd.DataFrame(res_1d, columns=cols), pd.DataFrame(res_4h, columns=cols), pd.DataFrame(res_15m, columns=cols)
    except Exception as e:
        logging.error(f"Błąd pobierania danych dla {symbol}: {e}")
        return symbol, None, None, None


# --- 6. GŁÓWNA PĘTLA APLIKACJI ---
async def main():
    start_time = time.time()
    logging.info("Uruchamianie Skanera Krypto (Tryb w 100% Instytucjonalny)...")
    
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    usd_gbp_rate = 0.78
    elapsed = 0.0

    async with httpx.AsyncClient() as http_client:
        try:
            btc_tasks = [
                fetch_ticker_safe(exchange, "GBP/USD"),
                fetch_ohlcv_retry_async(exchange, "BTC/GBP", "1d", 250),
                fetch_ohlcv_retry_async(exchange, "BTC/GBP", "4h", 300),
                fetch_ohlcv_retry_async(exchange, "BTC/GBP", "15m", 100),
                fetch_ohlcv_retry_async(exchange, "BTC/USD", "1d", 250),
                fetch_ohlcv_retry_async(exchange, "BTC/USD", "4h", 300),
                fetch_ohlcv_retry_async(exchange, "BTC/USD", "15m", 100),
            ]
            
            btc_results = await asyncio.gather(*btc_tasks, return_exceptions=True)
            if any(isinstance(res, Exception) for res in btc_results):
                raise RuntimeError("Błąd krytyczny pobierania danych bazowych BTC/Benchmark.")

            ticker_gbp, btc_gbp_1d, btc_gbp_4h, btc_gbp_15m, btc_usd_1d, btc_usd_4h, btc_usd_15m = btc_results

            if ticker_gbp and isinstance(ticker_gbp, dict) and ticker_gbp.get('last'):
                usd_gbp_rate = 1.0 / ticker_gbp['last']

            cols = ['ts', 'o', 'h', 'l', 'close', 'v']
            btc_cache = {
                "BTC/GBP": {
                    "1d": pd.DataFrame(btc_gbp_1d, columns=cols),
                    "4h": pd.DataFrame(btc_gbp_4h, columns=cols),
                    "15m": pd.DataFrame(btc_gbp_15m, columns=cols)
                },
                "BTC/USD": {
                    "1d": pd.DataFrame(btc_usd_1d, columns=cols),
                    "4h": pd.DataFrame(btc_usd_4h, columns=cols),
                    "15m": pd.DataFrame(btc_usd_15m, columns=cols)
                },
            }

            btc_macro_bullish = {}
            for btc_sym, btc_dfs in btc_cache.items():
                df_b_1d = btc_dfs["1d"]
                ema_200 = df_b_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                btc_macro_bullish[btc_sym] = bool(df_b_1d['close'].iloc[-1] > ema_200)

            fetch_tasks = [fetch_symbol_data(exchange, sym) for sym in SYMBOLS if sym not in btc_cache]
            fetched_results = await asyncio.gather(*fetch_tasks)

            results = []
            for sym in SYMBOLS:
                if sym in btc_cache:
                    results.append((
                        sym,
                        btc_cache[sym]["1d"],
                        btc_cache[sym]["4h"],
                        btc_cache[sym]["15m"]
                    ))
                else:
                    res = next((r for r in fetched_results if r[0] == sym), (sym, None, None, None))
                    results.append(res)

            digest_lines = []
            digest_summary_rows = []
            pending_alerts = []
            rows_to_append_sheets = []
            position_async_tasks = []

            active_positions = cache.get("active_positions", {})

            success_count = 0
            total_checked = len(SYMBOLS)

            for symbol, df_1d, df_4h, df_15m in results:
                if df_1d is None or len(df_1d) < 30 or len(df_4h) < 100 or len(df_15m) < 20:
                    continue

                is_core = symbol in CORE_CRYPTO
                quote_currency = symbol.split('/')[1]
                btc_symbol = f"BTC/{quote_currency}"

                df_1d, df_4h, df_15m = compute_indicators(df_1d, df_4h, df_15m)

                is_btc_macro_bullish = btc_macro_bullish.get(btc_symbol, True)
                df_4h_btc = btc_cache.get(btc_symbol, {}).get("4h")

                is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['EMA_200'].iloc[-1]
                ts_closed = int(df_4h['ts'].iloc[-2])
                adx_closed = df_4h['ADX'].iloc[-2]
                is_trending_4h = adx_closed > 25

                has_bullish_div = check_bullish_divergence(df_4h)

                is_strong_vs_btc_4h = True
                if symbol != btc_symbol and df_4h_btc is not None:
                    merged_rs = pd.merge(df_4h[['ts', 'close']], df_4h_btc[['ts', 'close']], on='ts', suffixes=('', '_btc'))
                    if len(merged_rs) > 20:
                        merged_rs.set_index('ts', inplace=True)
                        rs_series = merged_rs['close'] / (merged_rs['close_btc'] + 1e-10)
                        rs_ema = rs_series.ewm(span=20, adjust=False).mean()
                        
                        if ts_closed in merged_rs.index:
                            is_strong_vs_btc_4h = bool(rs_series.loc[ts_closed] >= rs_ema.loc[ts_closed])
                        else:
                            is_strong_vs_btc_4h = bool(rs_series.iloc[-1] >= rs_ema.iloc[-1])

                rsi_4h_closed = round(df_4h['RSI'].iloc[-2], 1)
                rsi_15m_closed = round(df_15m['RSI'].iloc[-2], 1)
                
                close_closed = df_4h['close'].iloc[-2]
                high_closed = df_4h['h'].iloc[-2]
                low_closed = df_4h['l'].iloc[-2]

                bb_upper_closed = df_4h['BB_upper'].iloc[-2]
                bb_lower_closed = df_4h['BB_lower'].iloc[-2]
                avg_atr = df_4h['ATR_MA'].iloc[-2]
                atr_val = df_4h['ATR'].iloc[-2] if not pd.isna(df_4h['ATR'].iloc[-2]) else 0.0
                bbw_closed = df_4h['BBW'].iloc[-2]
                bbw_avg = df_4h['BBW_MA'].iloc[-2]

                current_vol = df_4h['v'].iloc[-2]
                avg_vol = df_4h['Vol_MA'].iloc[-2]
                vol_multiplier = current_vol / avg_vol if not pd.isna(avg_vol) and avg_vol > 0 else 0
                
                is_green_candle_4h = df_4h['close'].iloc[-2] > df_4h['o'].iloc[-2]
                is_volume_spike = (vol_multiplier > 1.4) and is_green_candle_4h
                is_anomaly_candle = df_4h['ATR'].iloc[-2] > (avg_atr * 2.5) if not pd.isna(avg_atr) and avg_atr > 0 else False

                prev_close_price = float(df_1d['close'].iloc[-2]) if len(df_1d['close']) > 1 else close_closed
                flash_crash_pct = round(((close_closed - prev_close_price) / prev_close_price) * 100, 2)
                is_flash_crash = flash_crash_pct <= -3.5

                tp_target_raw = close_closed + (2.5 * atr_val) if is_uptrend_1d else bb_upper_closed

                if symbol.endswith("/USD"):
                    price_gbp = close_closed * usd_gbp_rate
                    high_gbp = high_closed * usd_gbp_rate
                    low_gbp = low_closed * usd_gbp_rate
                    atr_gbp = atr_val * usd_gbp_rate
                    display_price = f"£{price_gbp:.4f}" if price_gbp < 1 else f"£{price_gbp:.2f}"
                    sl_calc_gbp = max(0, min((close_closed - 1.5 * atr_val) * usd_gbp_rate, low_gbp))
                    tp_calc_gbp = tp_target_raw * usd_gbp_rate
                else:
                    price_gbp = close_closed
                    high_gbp = high_closed
                    low_gbp = low_closed
                    atr_gbp = atr_val
                    display_price = f"£{close_closed:.4f}" if close_closed < 1 else f"£{close_closed:.2f}"
                    sl_calc_gbp = max(0, min(close_closed - 1.5 * atr_val, low_gbp))
                    tp_calc_gbp = tp_target_raw

                sl_str = f"£{sl_calc_gbp:.4f}" if sl_calc_gbp < 1 else f"£{sl_calc_gbp:.2f}"
                tp_str = f"£{tp_calc_gbp:.4f}" if tp_calc_gbp < 1 else f"£{tp_calc_gbp:.2f}"
                tp_str += " (Trailing ATR)" if is_uptrend_1d else " (BB Upper)"

                position_gbp = calculate_position_size(price_gbp, sl_calc_gbp, FIXED_RISK_GBP)
                pos_size_str = f"£{position_gbp:.2f}" if position_gbp > 0 else "N/A"

                # WERYFIKACJA AKTYWNYCH POZYCJI
                if symbol in active_positions:
                    pos = active_positions[symbol]
                    entry_price = pos["entry_price"]
                    sl_price = pos["sl_price"]
                    tp1_price = pos.get("tp1_price", entry_price + (1.5 * atr_gbp))
                    pos_atr_gbp = pos.get("atr_gbp", atr_gbp)

                    pos["highest_price"] = max(pos.get("highest_price", price_gbp), high_gbp)

                    if is_flash_crash and price_gbp <= sl_price and not pos.get("crash_alert_sent", False):
                        crash_msg = (
                            f"🚨 **OSTRZEŻENIE: FLASH CRASH / ZAŁAMANIE DLA {symbol}!**\n\n"
                            f"Cena zanurkowała o `{flash_crash_pct}%`. Pozycja otwarta po `£{entry_price:.4f}` dotknęła poziomu Stop Loss (`£{sl_price:.4f}`)."
                        )
                        position_async_tasks.append(send_telegram_alert_async(http_client, crash_msg))
                        position_async_tasks.append(add_to_tasks_async(tasks_service, f"FLASH CRASH SL: {symbol}", crash_msg.replace('**', '')))
                        pos["crash_alert_sent"] = True

                    entry_date_str = pos.get("date", today_str)
                    try:
                        entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d').date()
                        days_open = (uk_now.date() - entry_date).days
                        if days_open >= 4 and not pos.get("stale_alert_sent", False):
                            stale_msg = (
                                f"⏳ **ZAMROŻONY KAPITAŁ DLA {symbol}!**\n\n"
                                f"Pozycja otwarta od {days_open} dni bez osiągnięcia TP1 ani SL.\n"
                                f"💰 Cena obecna: `{display_price}` (Wejście: `£{entry_price:.4f}`)\n"
                                f"💡 Rozważ zamknięcie z ręki i zwolnienie środków na inne altcoiny."
                            )
                            position_async_tasks.append(send_telegram_alert_async(http_client, stale_msg))
                            position_async_tasks.append(add_to_tasks_async(tasks_service, f"ZAMROŻONY KAPITAŁ: {symbol}", stale_msg.replace('**', '')))
                            pos["stale_alert_sent"] = True
                    except Exception as e:
                        logging.error(f"Błąd sprawdzania starych pozycji dla {symbol}: {e}")

                    if not pos.get("tp1_hit", False):
                        if high_gbp >= tp1_price:
                            tp1_msg = (
                                f"🎯 **TAKE PROFIT 1 OSIĄGNIĘTY dla {symbol}!**\n\n"
                                f"💰 **Cena:** `{display_price}`\n"
                                f"✅ **Zrealizuj 50% pozycji w zysku.**\n"
                                f"🛡 **Stop Loss przesunięty na Break-Even:** `£{entry_price:.4f}`\n\n"
                                f"Druga połowa pozycji przechodzi w tryb Trailing SL! 🚀"
                            )
                            position_async_tasks.append(send_telegram_alert_async(http_client, tp1_msg))
                            position_async_tasks.append(add_to_tasks_async(tasks_service, f"TP1: Zrealizuj 50% {symbol}", tp1_msg.replace('**', '')))
                            rows_to_append_sheets.append([now_str, symbol, "TP1_HIT_50% 🟢", display_price, f"RSI: {rsi_4h_closed}", "-", "-"])
                            
                            pos["tp1_hit"] = True
                            pos["sl_price"] = entry_price

                        elif low_gbp <= sl_price:
                            sl_msg = f"🔴 **STOP LOSS TRAFIONY dla {symbol}!**\nCena spadła do `{sl_str}`. Pozycja zamknięta."
                            position_async_tasks.append(send_telegram_alert_async(http_client, sl_msg))
                            position_async_tasks.append(add_to_tasks_async(tasks_service, f"SL TRAFIONY: {symbol}", sl_msg.replace('**', '')))
                            rows_to_append_sheets.append([now_str, symbol, "SL_HIT_CLOSED 🔴", display_price, f"RSI: {rsi_4h_closed}", "-", "-"])
                            del active_positions[symbol]

                    else:
                        new_trailing_sl = pos["highest_price"] - (2.0 * pos_atr_gbp)
                        if new_trailing_sl > pos["sl_price"]:
                            pos["sl_price"] = new_trailing_sl
                            logging.info(f"Podciągnięto Trailing SL dla {symbol} do poziomu £{new_trailing_sl:.4f}")

                        if low_gbp <= pos["sl_price"]:
                            closed_sl_str = f"£{pos['sl_price']:.4f}" if pos['sl_price'] < 1 else f"£{pos['sl_price']:.2f}"
                            trail_msg = (
                                f"💰 **TRAILING SL TRAFIONY dla {symbol}!**\n\n"
                                f"Cena zawróciła do poziomu `{closed_sl_str}`.\n"
                                f"✅ **Druga połowa pozycji zamknięta w dodatkowym zysku!**"
                            )
                            position_async_tasks.append(send_telegram_alert_async(http_client, trail_msg))
                            position_async_tasks.append(add_to_tasks_async(tasks_service, f"TRAILING SL: Zamknięto {symbol}", trail_msg.replace('**', '')))
                            rows_to_append_sheets.append([now_str, symbol, "TRAILING_SL_CLOSED 🟢", display_price, f"RSI: {rsi_4h_closed}", "-", "-"])
                            del active_positions[symbol]

                # PROGI SYGNAŁOWE RSI
                base_buy, base_sell = (28, 70) if is_core else (22, 78)
                confirm_15m_buy, confirm_15m_sell = (38, 62) if is_core else (32, 68)

                volatility_multiplier = (bbw_closed / bbw_avg) if (not pd.isna(bbw_avg) and bbw_avg > 0) else 1.0
                min_buy_floor = 22 if is_core else 18
                buy_rsi_threshold = max(min_buy_floor, base_buy - 4) if volatility_multiplier > 1.3 else (min(33, base_buy + 3) if volatility_multiplier < 0.7 else base_buy)
                sell_rsi_threshold = min(82, base_sell + 3) if volatility_multiplier > 1.3 else (max(65, base_sell - 3) if volatility_multiplier < 0.7 else base_sell)

                if is_uptrend_1d:
                    sell_rsi_threshold = max(sell_rsi_threshold, 75 if is_core else 80)

                crossover_up = (df_4h['EMA_20'].iloc[-3] <= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] > df_4h['EMA_50'].iloc[-2])
                crossover_down = (df_4h['EMA_20'].iloc[-3] >= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] < df_4h['EMA_50'].iloc[-2])
                
                is_oversold_4h = rsi_4h_closed <= buy_rsi_threshold
                is_overbought_4h = rsi_4h_closed >= sell_rsi_threshold
                
                if is_core:
                    is_base_buy = ((is_oversold_4h and rsi_15m_closed <= confirm_15m_buy) or (crossover_up and is_uptrend_1d) or (is_oversold_4h and close_closed <= bb_lower_closed) or has_bullish_div) and not is_anomaly_candle
                else:
                    is_base_buy = ((is_oversold_4h and (rsi_15m_closed <= confirm_15m_buy or close_closed <= bb_lower_closed)) or has_bullish_div) and not is_anomaly_candle

                if is_trending_4h and not is_uptrend_1d and not has_bullish_div:
                    is_base_buy = False
                if not is_core and not is_btc_macro_bullish and not has_bullish_div:
                    is_base_buy = False

                is_base_sell = ((is_overbought_4h and rsi_15m_closed >= confirm_15m_sell) or crossover_down or (is_overbought_4h and close_closed >= bb_upper_closed)) and not is_anomaly_candle

                current_signal_type = "NONE"
                if is_base_buy:
                    current_signal_type = "BUY_MEGA" if (is_volume_spike or is_core or has_bullish_div) else "BUY_SWING"
                elif is_base_sell:
                    current_signal_type = "SELL_TAKE_PROFIT" if (is_core or is_uptrend_1d) else "SELL_EVACUATION"

                status_map = {
                    "NONE": "⚪️ Neutralny",
                    "BUY_MEGA": "🟢 MEGA OKAZJA",
                    "BUY_SWING": "🟡 Dołek 4H",
                    "SELL_TAKE_PROFIT": "🟠 Take Profit",
                    "SELL_EVACUATION": "🔴 Ewakuacja"
                }
                status_txt = status_map.get(current_signal_type, "Neutralny")
                trend_txt = "↗️" if is_uptrend_1d else "↘️"
                div_tag = " | 📈 BYCZA DYWERGENCJA" if has_bullish_div else ""
                
                sym_link = f"[{symbol}](https://live.trading212.com/)"
                digest_lines.append(f"🪙 {sym_link}: {display_price} | RSI: {rsi_4h_closed} | {status_txt}{div_tag}\n")
                digest_summary_rows.append({
                    'symbol': symbol, 'price': display_price,
                    'rsi': rsi_4h_closed, 'adx': round(adx_closed,1),
                    'trend': trend_txt, 'status': status_txt, 'div_tag': div_tag
                })

                symbol_cache = cache.get(symbol, {})
                if not isinstance(symbol_cache, dict):
                    symbol_cache = {}

                last_ts = symbol_cache.get("ts", 0)
                last_signal = symbol_cache.get("signal", "NONE")
                last_alert_time = symbol_cache.get("last_alert_time", 0.0)

                current_epoch = time.time()
                is_cooldown_active = (current_signal_type == last_signal) and (current_epoch - last_alert_time < SIGNAL_COOLDOWN_SECONDS)

                should_alert = False
                if current_signal_type != "NONE" and not is_cooldown_active:
                    if last_ts != ts_closed or last_signal != current_signal_type:
                        should_alert = True

                signal_to_save = current_signal_type if current_signal_type != "NONE" else (last_signal if last_ts == ts_closed else "NONE")

                if should_alert:
                    t212_link = "[💼 Handluj na Trading 212](https://live.trading212.com/)"
                    div_note = "\n📈 **Wykryto BYCZĄ DYWERGENCJĘ RSI (4H)!**\n" if has_bullish_div else ""
                    risk_label = int(FIXED_RISK_GBP) if FIXED_RISK_GBP.is_integer() else FIXED_RISK_GBP

                    if current_signal_type in ["BUY_MEGA", "BUY_SWING"]:
                        tp1_calc_gbp = price_gbp + (1.5 * atr_gbp)
                        active_positions[symbol] = {
                            "entry_price": price_gbp,
                            "sl_price": sl_calc_gbp,
                            "tp1_price": tp1_calc_gbp,
                            "tp1_hit": False,
                            "highest_price": high_gbp,
                            "atr_gbp": atr_gbp,
                            "date": today_str,
                            "stale_alert_sent": False,
                            "crash_alert_sent": False
                        }

                    if current_signal_type == "BUY_MEGA":
                        rodzaj = "🟢 **MEGA OKAZJA CORE / HOSSA (KUPNO DOŁKA)**"
                        task_title = f"MEGA OKAZJA: KUP {symbol}"
                        msg = (
                            f"{rodzaj}\n{div_note}\n"
                            f"🪙 **Moneta:** `{symbol}`\n"
                            f"💰 **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {buy_rsi_threshold}` | **ADX 4H:** `{round(adx_closed, 1)}`\n"
                            f"💪 **Typ monety:** {'FILAR CORE 🚀' if is_core else 'Satelita 🛰'}\n"
                            f"📈 **Trend 1D:** {'Wzrostowy 🟢' if is_uptrend_1d else 'Korekta w Bessie (Okazja Core) 🟡'}\n"
                            f"⚖️ **Rekomendowana wielkość pozycji (Ryzyko £{risk_label}):** `{pos_size_str}`\n"
                            f"🛡 **Sugerowany Stop Loss:** `{sl_str}`\n🎯 **Sugerowany TP1 (50%):** `£{tp1_calc_gbp:.4f}`\n\n"
                            f"🔗 {t212_link}"
                        )
                    elif current_signal_type == "BUY_SWING":
                        rodzaj = "🟡 **OKAZJA SWING / SATELITA (LOKALNY DOŁEK)**"
                        task_title = f"SWING: SPRAWDŹ {symbol}"
                        msg = (
                            f"{rodzaj}\n{div_note}\n"
                            f"🪙 **Moneta:** `{symbol}`\n"
                            f"💰 **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {buy_rsi_threshold}` | **ADX 4H:** `{round(adx_closed, 1)}`\n"
                            f"💪 **Siła Wzgl. (BTC):** {'Outperformer 🟢' if is_strong_vs_btc_4h else 'Neutralna/Słabsza 🟡'}\n"
                            f"📈 **Trend 1D:** {'Wzrostowy 🟢' if is_uptrend_1d else 'Spadkowy 🔴'}\n"
                            f"⚖️ **Rekomendowana wielkość pozycji (Ryzyko £{risk_label}):** `{pos_size_str}`\n"
                            f"🛡 **Sugerowany Stop Loss:** `{sl_str}`\n🎯 **Sugerowany TP1 (50%):** `£{tp1_calc_gbp:.4f}`\n\n"
                            f"🔗 {t212_link}"
                        )
                    elif current_signal_type == "SELL_TAKE_PROFIT":
                        rodzaj = "🟠 **LOKALNE WYKUPIENIE: REALIZUJ ZYSKI (TAKE PROFIT)**"
                        task_title = f"TAKE PROFIT: {symbol} (RSI {rsi_4h_closed})"
                        msg = (
                            f"{rodzaj}\n\n"
                            f"🪙 **Moneta:** `{symbol}`\n"
                            f"💰 **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {sell_rsi_threshold}`\n"
                            f"💪 **Typ monety:** {'FILAR CORE 🚀' if is_core else 'Satelita 🛰'}\n\n"
                            f"🔗 {t212_link}"
                        )
                    else: 
                        rodzaj = "🔴 KRYTYCZNA EWAKUACJA: SPRZEDAŻ SATELITY W TRENDZIE SPADKOWYM!"
                        task_title = f"🔴 PILNE: SPRZEDAJ {symbol.upper()}"
                        msg = (
                            f"🚨 **{rodzaj}**\n\n"
                            f"🔴 **MONETA: {symbol.upper()}**\n"
                            f"🔴 **CENA: {display_price.upper()}**\n"
                            f"🔴 **RSI 4H: {rsi_4h_closed} (PRÓG: {sell_rsi_threshold})**\n"
                            f"🔴 **TREND 1D: SPADKOWY**\n\n"
                            f"🔗 {t212_link}"
                        )

                    pending_alerts.append((
                        task_title, msg, symbol, status_txt,
                        {
                            'symbol': symbol,
                            'signal': current_signal_type,
                            'price': display_price,
                            'rsi': rsi_4h_closed,
                            'sl': sl_str,
                            'tp': tp_str
                        }
                    ))

                cache[symbol] = {
                    "ts": ts_closed, 
                    "signal": signal_to_save,
                    "last_alert_time": current_epoch if should_alert else last_alert_time
                }
                success_count += 1

            cache["active_positions"] = active_positions

            if position_async_tasks:
                await asyncio.gather(*position_async_tasks)

            elapsed = round(time.time() - start_time, 2)
            health_summary = f"🩺 **Autodiagnostyka Krypto:** Przeanalizowano `{success_count}/{total_checked}` par w `{elapsed}s`. System operacyjny stabilny."
            logging.info(health_summary.replace('**', '').replace('`', ''))

            write_github_step_summary(digest_summary_rows, health_msg=health_summary)
            await process_telegram_commands_async(http_client, cache, digest_summary_rows)

            if pending_alerts:
                alert_coroutines = []
                if len(pending_alerts) <= 3:
                    for task_title, msg, _, _, meta in pending_alerts:
                        alert_coroutines.append(send_telegram_alert_async(http_client, msg))
                        alert_coroutines.append(add_to_tasks_async(tasks_service, task_title, msg.replace('**', '')))
                        rows_to_append_sheets.append([
                            now_str, meta['symbol'], meta['signal'],
                            meta['price'], f"RSI: {meta['rsi']}",
                            f"SL: {meta['sl']}", f"TP: {meta['tp']}"
                        ])
                else:
                    lawina_title = f"🚨 LAWINOWA ZMIANA NA RYNKU: {len(pending_alerts)} ALERTÓW!"
                    lawina_msg = f"⚠️ **ZMASOWANA ZMIANA STANU RYNKU ({len(pending_alerts)} MONET)**\n\n"
                    for _, _, sym, stat, meta in pending_alerts:
                        lawina_msg += f"• **{sym}**: {stat}\n"
                        rows_to_append_sheets.append([
                            now_str, meta['symbol'], meta['signal'],
                            meta['price'], f"RSI: {meta['rsi']}",
                            f"SL: {meta['sl']}", f"TP: {meta['tp']}"
                        ])
                    lawina_msg += "\n*Sprawdź szczegóły w aplikacji lub poczekaj na digest.*"
                    
                    alert_coroutines.append(send_telegram_alert_async(http_client, lawina_msg))
                    alert_coroutines.append(add_to_tasks_async(tasks_service, lawina_title, lawina_msg.replace('**', '')))

                await asyncio.gather(*alert_coroutines)

            if sheet and rows_to_append_sheets:
                try:
                    await asyncio.to_thread(sheet.append_rows, rows_to_append_sheets)
                    logging.info(f"Zapisano {len(rows_to_append_sheets)} wierszy w Google Sheets.")
                except Exception as e:
                    logging.error(f"Nie udało się zapisać wierszy w Google Sheets: {e}")

            if uk_now.hour >= 21 and cache.get("DIGEST_DATE") != today_str:
                if digest_lines:
                    digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n" + "".join(digest_lines) + "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                    await asyncio.gather(
                        send_telegram_alert_async(http_client, digest_msg),
                        add_to_tasks_async(tasks_service, f"Codzienny Digest ({today_str})", digest_msg.replace('**', ''))
                    )
                    cache["DIGEST_DATE"] = today_str

            save_cache(cache)

        finally:
            await exchange.close()

    logging.info(f"Skaner Krypto zakończył działanie w czasie: {elapsed}s.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"KRYTYCZNY BŁĄD SKANERA KRYPTO: {e}", exc_info=True)
        err_msg = f"🚨 **🔴 KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`"
        
        async def send_critical_error():
            async with httpx.AsyncClient() as http_client:
                await send_telegram_alert_async(http_client, err_msg)
        
        asyncio.run(send_critical_error())
        raise e
