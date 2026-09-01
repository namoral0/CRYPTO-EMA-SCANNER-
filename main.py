import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
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

FIXED_RISK_NATIVE = 20.0  # £20 dla GBP / $20 dla USD

CORE_CRYPTO = ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
SYMBOLS = [
    "TAO/USD", "BTC/GBP", "ETH/GBP", "SOL/GBP", 
    "XRP/GBP", "RENDER/USD", "SUI/GBP", "LINK/GBP", "AAVE/GBP"
]
CACHE_FILE = "cache_krypto.json"

KRAKEN_SEMAPHORE = asyncio.Semaphore(3)
GOOGLE_SEMAPHORE = asyncio.Semaphore(2)

SIGNAL_COOLDOWN_SECONDS = 86400


# --- 3. INICJALIZACJA USŁUG I I/O ---
def init_google_services() -> Tuple[Any, Optional[Any]]:
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

def load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "active_positions" not in data:
                    data["active_positions"] = {}
                return data
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {"active_positions": {}}

def save_cache(cache_data: Dict[str, Any]) -> None:
    """Zapis atomowy cache zapobiegający uszkodzeniu pliku JSON przy nagłym przerwaniu skryptu."""
    try:
        dir_name = os.path.dirname(os.path.abspath(CACHE_FILE)) or "."
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(cache_data, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, CACHE_FILE)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache (zapis atomowy): {e}")

async def send_telegram_alert_async(http_client: httpx.AsyncClient, msg: str, custom_chat_id: Optional[str] = None) -> None:
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

async def add_to_tasks_async(tasks_service: Any, title: str, notes: str) -> None:
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

def write_github_step_summary(digest_rows: List[Dict[str, Any]], health_msg: str = "") -> None:
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                if health_msg:
                    f.write(f"> {health_msg}\n\n")
                f.write("### 📊 Podsumowanie Skanera Krypto (Tryb Hybrydowy)\n\n")
                f.write("| Moneta | Cena | RSI 4H | ADX 4H | Trend 1D | Stan |\n")
                f.write("| --- | --- | --- | --- | --- | --- |\n")
                for r in digest_rows:
                    f.write(f"| **{r['symbol']}** | {r['price']} | {r['rsi']} | {r['adx']} | {r['trend']} | {r['status']} |\n")
        except Exception as e:
            logging.error(f"Błąd zapisu GITHUB_STEP_SUMMARY: {e}")

async def process_telegram_commands_async(http_client: httpx.AsyncClient, cache: Dict[str, Any], digest_rows: List[Dict[str, Any]]) -> None:
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
                    status_msg = "📋 **AKTUALNY STAN RYNKU (KRYPTO - HYBRYDA)**\n\n"
                    for r in digest_rows:
                        sym_link = f"[{r['symbol']}](https://live.trading212.com/)"
                        div = r.get('div_tag', '')
                        status_msg += f"🪙 {sym_link}: {r['price']} | RSI: {r['rsi']} | {r['status']}{div}\n"
                    status_msg += "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                    target_chat = chat_id or TELEGRAM_CHAT_ID
                    if target_chat:
                        await send_telegram_alert_async(http_client, status_msg, custom_chat_id=target_chat)
    except Exception as e:
        logging.error(f"Błąd przetwarzania komend Telegram: {e}")


# --- 4. FUNKCJE ANALITYCZNE I MATEMATYCZNE ---
def check_bullish_divergence(df_4h: pd.DataFrame, lookback: int = 35) -> bool:
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
        rsi_oversold = curr_pivot[2] < 48
        valid_distance = 3 <= (curr_pivot[0] - prev_pivot[0]) <= 25
        is_recent_pivot = (n - 1 - curr_pivot[0]) <= 5

        return bool(price_lower and rsi_higher and rsi_oversold and valid_distance and is_recent_pivot)
    except Exception:
        return False

def calculate_position_size(price: float, sl_price: float, fixed_risk: float = FIXED_RISK_NATIVE) -> float:
    try:
        if price <= sl_price or sl_price <= 0:
            return 0.0
        risk_pct = (price - sl_price) / price
        if risk_pct <= 0:
            return 0.0
        return round(fixed_risk / risk_pct, 2)
    except Exception:
        return 0.0

def compute_indicators(df_1d: pd.DataFrame, df_4h: pd.DataFrame, df_15m: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    # 1D Indicators
    df_1d['EMA_200'] = df_1d['close'].ewm(span=200, adjust=False).mean()
    delta_1d = df_1d['close'].diff()
    gain_1d = delta_1d.where(delta_1d > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_1d = (-delta_1d.where(delta_1d < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_1d['RSI'] = 100 - (100 / (1 + (gain_1d / loss_1d)))

    # 4H Indicators
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

    # 15M Indicators
    delta_15m = df_15m['close'].diff()
    gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_15m['RSI'] = 100 - (100 / (1 + (gain_15m / loss_15m)))

    return df_1d, df_4h, df_15m


# --- 5. ASYNCHRONICZNE POBIERANIE DANYCH ---
async def fetch_ohlcv_retry_async(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 250, retries: int = 3, delay: float = 2.0) -> List[Any]:
    async with KRAKEN_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except (ccxt.NetworkError, ccxt.RateLimitExceeded, ccxt.RequestTimeout) as e:
                if attempt == retries - 1:
                    logging.error(f"Wypróbowano {retries} prób dla {symbol} ({timeframe}). Ostatni błąd: {e}")
                    raise e
                sleep_time = delay * (2 ** attempt)
                logging.warning(f"Timeout z Krakena dla {symbol} [{timeframe}] (Próba {attempt + 1}/{retries}). Odczekanie {sleep_time}s...")
                await asyncio.sleep(sleep_time)
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep(delay)

async def fetch_symbol_data(exchange: ccxt.Exchange, symbol: str) -> Tuple[str, Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
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
async def main() -> None:
    start_time = time.time()
    logging.info("Uruchamianie Skanera Krypto (Tryb Hybrydowy - Professional Grade)...")
    
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    
    exchange = ccxt.kraken({
        'enableRateLimit': True,
        'timeout': 15000
    })
    elapsed = 0.0

    async with httpx.AsyncClient() as http_client:
        try:
            fetch_tasks = [fetch_symbol_data(exchange, sym) for sym in SYMBOLS]
            results = await asyncio.gather(*fetch_tasks)

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
                df_1d, df_4h, df_15m = await asyncio.to_thread(compute_indicators, df_1d, df_4h, df_15m)

                is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['EMA_200'].iloc[-1]
                is_uptrend_4h = df_4h['close'].iloc[-2] > df_4h['EMA_50'].iloc[-2]
                ts_closed = int(df_4h['ts'].iloc[-2])
                adx_closed = df_4h['ADX'].iloc[-2]

                has_bullish_div = await asyncio.to_thread(check_bullish_divergence, df_4h)

                rsi_4h_closed = round(float(df_4h['RSI'].iloc[-2]), 1)
                rsi_1d_closed = round(float(df_1d['RSI'].iloc[-1]), 1)
                
                close_closed = float(df_4h['close'].iloc[-2])
                low_closed = float(df_4h['l'].iloc[-2])

                bb_lower_closed = float(df_4h['BB_lower'].iloc[-2])
                bb_upper_closed = float(df_4h['BB_upper'].iloc[-2]) if not pd.isna(df_4h['BB_upper'].iloc[-2]) else close_closed * 1.02
                avg_atr = float(df_4h['ATR_MA'].iloc[-2]) if not pd.isna(df_4h['ATR_MA'].iloc[-2]) else 0.0
                atr_val = float(df_4h['ATR'].iloc[-2]) if not pd.isna(df_4h['ATR'].iloc[-2]) else 0.0

                is_anomaly_candle = atr_val > (avg_atr * 2.5) if avg_atr > 0 else False

                sl_multiplier = 2.0
                curr_symbol_native = "$" if symbol.endswith("/USD") else "£"

                display_price = f"{curr_symbol_native}{close_closed:.4f}" if close_closed < 1 else f"{curr_symbol_native}{close_closed:.2f}"
                sl_calc = max(0.0, min(close_closed - sl_multiplier * atr_val, low_closed))
                sl_str = f"{curr_symbol_native}{sl_calc:.4f}" if sl_calc < 1 else f"{curr_symbol_native}{sl_calc:.2f}"
                
                tp_calc = bb_upper_closed
                tp_str = f"{curr_symbol_native}{tp_calc:.4f} (BB Upper)" if tp_calc < 1 else f"{curr_symbol_native}{tp_calc:.2f} (BB Upper)"

                position_size = calculate_position_size(close_closed, sl_calc, FIXED_RISK_NATIVE)
                pos_size_str = f"{curr_symbol_native}{position_size:.2f}" if position_size > 0 else "N/A"

                # Wykrywanie Stop Loss
                if symbol in active_positions:
                    pos = active_positions[symbol]
                    sl_price = pos["sl_price"]
                    if low_closed <= sl_price:
                        sl_disp = f"{curr_symbol_native}{sl_price:.4f}" if sl_price < 1 else f"{curr_symbol_native}{sl_price:.2f}"
                        low_disp = f"{curr_symbol_native}{low_closed:.4f}" if low_closed < 1 else f"{curr_symbol_native}{low_closed:.2f}"
                        sl_msg = (
                            f"🔴 **STOP LOSS TRAFIONY dla {symbol}!**\n"
                            f"Pozycja z dnia: `{pos.get('date', 'N/A')}`\n"
                            f"Cena spadła do: `{low_disp}` (Ustawiony SL: `{sl_disp}`)"
                        )
                        position_async_tasks.append(send_telegram_alert_async(http_client, sl_msg))
                        del active_positions[symbol]

                current_signal_type = "NONE"
                rsi_trend_threshold = 45.0 if is_core else 28.0
                is_strong_trend = adx_closed >= 20.0

                if not is_anomaly_candle:
                    if is_uptrend_1d and is_uptrend_4h and rsi_4h_closed <= rsi_trend_threshold and is_strong_trend:
                        current_signal_type = "BUY_TREND"
                    elif has_bullish_div or rsi_4h_closed <= 25.0 or close_closed <= bb_lower_closed:
                        current_signal_type = "BUY_REBOUND"

                status_map = {
                    "NONE": "⚪️ Neutralny",
                    "BUY_TREND": "🟢 KUPNO W TRENDZIE",
                    "BUY_REBOUND": "⚡️ SZYBKIE ODBICIE (REBOUND)",
                }
                status_txt = status_map.get(current_signal_type, "Neutralny")
                trend_txt = "↗️ Wzrostowy" if is_uptrend_1d else "↘️ Spadkowy"
                div_tag = " | 📈 BYCZA DYWERGENCJA" if has_bullish_div else ""
                
                sym_link = f"[{symbol}](https://live.trading212.com/)"
                digest_lines.append(f"🪙 {sym_link}: {display_price} | RSI: {rsi_4h_closed} | Trend: {trend_txt} | {status_txt}{div_tag}\n")
                digest_summary_rows.append({
                    'symbol': symbol, 'price': display_price,
                    'rsi': rsi_4h_closed, 'adx': round(float(adx_closed), 1),
                    'trend': trend_txt, 'status': status_txt, 'div_tag': div_tag
                })

                symbol_cache = cache.get(symbol, {})
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

                    if current_signal_type in ["BUY_TREND", "BUY_REBOUND"]:
                        active_positions[symbol] = {
                            "entry_price": close_closed,
                            "sl_price": sl_calc,
                            "date": today_str
                        }

                    if current_signal_type == "BUY_TREND":
                        task_title = f"TREND KUPNO: {symbol}"
                        msg = (
                            f"🟢 **STABILNE KUPNO W TRENDZIE WZROSTOWYM**\n\n"
                            f"🪙 **Moneta:** `{symbol}` | **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed}` | **ADX 4H:** `{round(float(adx_closed), 1)}`\n"
                            f"📈 **Trend 1D/4H:** `Wzrostowy 🟢`\n"
                            f"⚖️ **Pozycja (Ryzyko {curr_symbol_native}{int(FIXED_RISK_NATIVE)}):** `{pos_size_str}`\n"
                            f"🛡 **Stop Loss:** `{sl_str}`\n\n"
                            f"💡 *Zrealizuj zysk z ręki na Trading 212!*\n\n🔗 {t212_link}"
                        )
                    else:
                        task_title = f"⚡️ SZYBKIE ODBICIE: {symbol}"
                        if has_bullish_div:
                            rsi_desc = f"`{rsi_4h_closed}` (Potwierdzone Byczą Dywergencją 📈)"
                        elif rsi_4h_closed <= 25.0:
                            rsi_desc = f"`{rsi_4h_closed}` (Mocne wyprzedanie 📉)"
                        else:
                            rsi_desc = f"`{rsi_4h_closed}` (Strefa zwrotna / Wstęgi Bollingera)"

                        msg = (
                            f"⚡️ **SZYBKA OKAZJA NA ODBICIE (REBOUND)**{div_tag}\n\n"
                            f"🪙 **Moneta:** `{symbol}` | **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** {rsi_desc}\n"
                            f"📈 **Trend 1D:** `{'Wzrostowy 🟢' if is_uptrend_1d else 'Spadkowy/Korekta 🟡'}`\n"
                            f"⚖️ **Pozycja (Ryzyko {curr_symbol_native}{int(FIXED_RISK_NATIVE)}):** `{pos_size_str}`\n"
                            f"🛡 **Stop Loss:** `{sl_str}`\n\n"
                            f"⚠️ *To szybki swing! Jak zobaczysz zielony wynik, natychmiast zgarnij zysk do ręki!*\n\n🔗 {t212_link}"
                        )

                    pending_alerts.append((
                        task_title, msg, symbol, status_txt,
                        {
                            'symbol': symbol, 
                            'status': status_txt,
                            'price': display_price, 
                            'rsi_4h': rsi_4h_closed,
                            'sl_str': sl_str,
                            'tp_str': tp_str
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
            health_summary = f"🩺 **Autodiagnostyka Krypto:** Przeanalizowano `{success_count}/{total_checked}` par w `{elapsed}s`."
            logging.info(health_summary.replace('**', '').replace('`', ''))

            write_github_step_summary(digest_summary_rows, health_msg=health_summary)
            await process_telegram_commands_async(http_client, cache, digest_summary_rows)

            if pending_alerts:
                alert_coroutines = []
                for task_title, msg, _, _, meta in pending_alerts:
                    alert_coroutines.append(send_telegram_alert_async(http_client, msg))
                    alert_coroutines.append(add_to_tasks_async(tasks_service, task_title, msg.replace('**', '')))
                    
                    rows_to_append_sheets.append([
                        now_str,            # Kolumna A: Data i czas
                        meta['symbol'],     # Kolumna B: Moneta
                        meta['status'],     # Kolumna C: Sygnał
                        meta['price'],      # Kolumna D: Cena
                        f"RSI: {meta['rsi_4h']}", # Kolumna E: RSI
                        f"SL: {meta['sl_str']}",  # Kolumna F: Stop Loss
                        f"TP: {meta['tp_str']}"   # Kolumna G: Take Profit
                    ])
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
        
        async def send_critical_error() -> None:
            async with httpx.AsyncClient() as http_client:
                await send_telegram_alert_async(http_client, err_msg)
        
        asyncio.run(send_critical_error())
        raise e
