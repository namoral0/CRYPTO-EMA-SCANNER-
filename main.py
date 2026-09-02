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

# BEZPIECZNE WYMUSZENIE DOMYŚLNEJ LISTY GOOGLE TASKS
GOOGLE_TASK_LIST_ID = os.getenv("GOOGLE_TASK_LIST_ID") or "@default"
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1XoG-AYYK06BNDmRYrtBvR2MdLKIcH638JnjYKnuV3pk")

CORE_CRYPTO = ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
SYMBOLS = [
    "TAO/GBP", "RENDER/GBP", "ONDO/GBP",
    "BTC/GBP", "ETH/GBP", "SOL/GBP", 
    "XRP/GBP", "SUI/GBP", "LINK/GBP", "AAVE/GBP"
]
CACHE_FILE = "cache_krypto.json"

KRAKEN_SEMAPHORE = asyncio.Semaphore(5)
GOOGLE_SEMAPHORE = asyncio.Semaphore(2)


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
                return json.load(f)
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {}

def save_cache(cache_data: Dict[str, Any]) -> None:
    try:
        dir_name = os.path.dirname(os.path.abspath(CACHE_FILE)) or "."
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(cache_data, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, CACHE_FILE)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache: {e}")

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
                f.write("### 📊 Podsumowanie Skanera Krypto\n\n")
                f.write("| Moneta | Cena (£) | RSI 4H | Formacja | Trend 1D | Stan |\n")
                f.write("| --- | --- | --- | --- | --- | --- |\n")
                for r in digest_rows:
                    f.write(f"| **{r['symbol']}** | {r['price']} | {r['rsi']} | {r['pinbar']} | {r['trend']} | {r['status']} |\n")
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
                    status_msg = "📋 **AKTUALNY STAN RYNKU (KRYPTO - GBP)**\n\n"
                    for r in digest_rows:
                        sym = r['symbol']
                        tv_ticker = sym.replace("/", "")
                        sym_link = f"[{sym}](https://www.tradingview.com/chart/?symbol=KRAKEN:{tv_ticker})"
                        status_msg += f"🪙 {sym_link}: {r['price']} | RSI: {r['rsi']} | {r['pinbar']} | {r['status']}\n"
                    status_msg += "\n📎 💼 [Handluj na Trading 212](https://live.trading212.com/)"
                    target_chat = chat_id or TELEGRAM_CHAT_ID
                    if target_chat:
                        await send_telegram_alert_async(http_client, status_msg, custom_chat_id=target_chat)
    except Exception as e:
        logging.error(f"Błąd przetwarzania komend Telegram: {e}")


# --- 4. FUNKCJE ANALITYCZNE ---
def check_pinbar_4h(df_4h: pd.DataFrame) -> bool:
    try:
        if len(df_4h) < 2:
            return False
        row = df_4h.iloc[-2]
        open_p, high_p, low_p, close_p = row['o'], row['h'], row['l'], row['close']
        candle_range = high_p - low_p
        if candle_range <= 0:
            return False
        lower_wick = min(open_p, close_p) - low_p
        return (lower_wick / candle_range) >= 0.40
    except Exception:
        return False

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

def compute_indicators(df_1d: pd.DataFrame, df_4h: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df_1d['EMA_200'] = df_1d['close'].ewm(span=200, adjust=False).mean()

    df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
    
    delta_4h = df_4h['close'].diff()
    gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_4h['RSI'] = 100 - (100 / (1 + (gain_4h / loss_4h)))
    
    df_4h['BB_mid'] = df_4h['close'].rolling(20).mean()
    df_4h['BB_std'] = df_4h['close'].rolling(20).std()
    df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
    df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)
    df_4h['BBW'] = (df_4h['BB_upper'] - df_4h['BB_lower']) / df_4h['BB_mid'].replace(0, 1e-10)
    df_4h['BBW_MA'] = df_4h['BBW'].rolling(20).mean()

    return df_1d, df_4h


# --- 5. POBIERANIE DANYCH Z AUTOMATYCZNYM KURSOWANIEM USD -> GBP ---
async def get_valid_symbol(exchange: ccxt.Exchange, symbol: str) -> str:
    if not exchange.markets:
        await exchange.load_markets()
    
    if symbol in exchange.markets:
        return symbol
    
    usd_symbol = symbol.replace("/GBP", "/USD")
    if usd_symbol in exchange.markets:
        return usd_symbol
    
    return symbol

async def fetch_ohlcv_retry_async(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 250, retries: int = 4, delay: float = 1.0) -> List[Any]:
    async with KRAKEN_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep(delay * (attempt + 1))

async def fetch_symbol_data(exchange: ccxt.Exchange, symbol: str) -> Tuple[str, str, Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    try:
        active_symbol = await get_valid_symbol(exchange, symbol)
        res_1d, res_4h = await asyncio.gather(
            fetch_ohlcv_retry_async(exchange, active_symbol, "1d", 250),
            fetch_ohlcv_retry_async(exchange, active_symbol, "4h", 300)
        )
        cols = ['ts', 'o', 'h', 'l', 'close', 'v']
        return symbol, active_symbol, pd.DataFrame(res_1d, columns=cols), pd.DataFrame(res_4h, columns=cols)
    except Exception as e:
        logging.error(f"Błąd pobierania danych dla {symbol}: {e}")
        return symbol, symbol, None, None


# --- 6. GŁÓWNA PĘTLA ---
async def main() -> None:
    start_time = time.time()
    logging.info("Uruchamianie Skanera Krypto...")
    
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    
    exchange = ccxt.kraken({'enableRateLimit': True, 'timeout': 15000})
    elapsed = 0.0

    async with httpx.AsyncClient() as http_client:
        try:
            await exchange.load_markets()

            # Pobieramy aktualny kurs przeliczeniowy USD -> GBP z Krakena
            usd_to_gbp_rate = 0.77  # Rezerwowy przelicznik
            try:
                gbp_usd_ticker = await exchange.fetch_ticker("GBP/USD")
                if gbp_usd_ticker and gbp_usd_ticker.get('last'):
                    usd_to_gbp_rate = 1.0 / float(gbp_usd_ticker['last'])
            except Exception as e:
                logging.warning(f"Nie udało się pobrać kursu GBP/USD z Krakena, używam domyślnego: {e}")

            results = await asyncio.gather(*[fetch_symbol_data(exchange, sym) for sym in SYMBOLS])

            digest_lines = []
            digest_summary_rows = []
            pending_alerts = []
            rows_to_append_sheets = []

            for original_symbol, active_symbol, df_1d, df_4h in results:
                if df_1d is None or len(df_1d) < 30 or len(df_4h) < 100:
                    continue

                is_core = original_symbol in CORE_CRYPTO
                is_usd_pair = active_symbol.endswith("/USD")

                # Jeśli pobrano parę USD, przeliczamy ceny na GBP
                conversion_factor = usd_to_gbp_rate if is_usd_pair else 1.0

                df_1d, df_4h = await asyncio.to_thread(compute_indicators, df_1d, df_4h)

                is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['EMA_200'].iloc[-1]
                is_uptrend_4h = df_4h['close'].iloc[-2] > df_4h['EMA_50'].iloc[-2]
                ts_closed = int(df_4h['ts'].iloc[-2])

                has_bullish_div = await asyncio.to_thread(check_bullish_divergence, df_4h)
                has_pinbar = check_pinbar_4h(df_4h)

                rsi_4h_closed = round(float(df_4h['RSI'].iloc[-2]), 1)
                close_closed_usd = float(df_4h['close'].iloc[-2])
                bb_lower_closed_usd = float(df_4h['BB_lower'].iloc[-2])

                # Przeprowadzamy konwersję cenową na GBP dla alertu
                close_closed = close_closed_usd * conversion_factor

                bbw_curr = df_4h['BBW'].iloc[-2]
                bbw_ma = df_4h['BBW_MA'].iloc[-2]
                is_high_volatility = bbw_curr > bbw_ma

                base_thr = 36.0 if is_core else 28.0
                buy_threshold = base_thr if is_high_volatility else (base_thr - 3.0)

                display_price = f"£{close_closed:.4f}" if close_closed < 1 else f"£{close_closed:.2f}"

                current_signal_type = "NONE"
                if is_uptrend_1d and is_uptrend_4h and rsi_4h_closed <= buy_threshold:
                    current_signal_type = "BUY_TREND"
                elif has_bullish_div or rsi_4h_closed <= (buy_threshold - 3.0) or close_closed_usd <= bb_lower_closed_usd or (has_pinbar and rsi_4h_closed <= 40):
                    current_signal_type = "BUY_REBOUND"

                status_map = {
                    "NONE": "⚪️ Neutralny",
                    "BUY_TREND": "🟢 KUPNO W TRENDZIE",
                    "BUY_REBOUND": "⚡️ SZYBKIE ODBICIE",
                }
                status_txt = status_map.get(current_signal_type, "Neutralny")
                trend_txt = "↗️ Wzrostowy" if is_uptrend_1d else "↘️ Spadkowy"
                pinbar_txt = "🕯️ Pinbar 4H" if has_pinbar else "Brak"

                # Wyświetlamy zawsze jako nazwa_symbolu/GBP dla Trading 212
                tv_ticker = active_symbol.replace("/", "")
                tv_link = f"[📈 Zobacz wykres na TradingView](https://www.tradingview.com/chart/?symbol=KRAKEN:{tv_ticker})"
                t212_link = "[💼 Handluj na Trading 212](https://live.trading212.com/)"

                sym_link = f"[{original_symbol}](https://live.trading212.com/)"
                digest_lines.append(f"🪙 {sym_link}: {display_price} | RSI: {rsi_4h_closed} | {pinbar_txt} | {status_txt}\n")
                digest_summary_rows.append({
                    'symbol': original_symbol, 'price': display_price,
                    'rsi': rsi_4h_closed, 'pinbar': pinbar_txt,
                    'trend': trend_txt, 'status': status_txt
                })

                symbol_cache = cache.get(original_symbol, {})
                last_ts = symbol_cache.get("ts", 0)
                last_signal = symbol_cache.get("signal", "NONE")
                last_alert_time = symbol_cache.get("last_alert_time", 0.0)
                last_sent_rsi = symbol_cache.get("last_sent_rsi", 0.0)
                last_sent_price = symbol_cache.get("last_sent_price", 0.0)

                current_epoch = time.time()
                rsi_changed = abs(rsi_4h_closed - last_sent_rsi) >= 0.5
                price_changed = abs(close_closed - last_sent_price) > (0.002 * close_closed if last_sent_price > 0 else 0.0)

                should_alert = False
                if current_signal_type != "NONE":
                    if last_ts != ts_closed or last_signal != current_signal_type:
                        should_alert = True
                    elif (rsi_changed or price_changed) and (current_epoch - last_alert_time >= 1800):
                        should_alert = True

                signal_to_save = current_signal_type if current_signal_type != "NONE" else (last_signal if last_ts == ts_closed else "NONE")

                if should_alert:
                    confirmations = []
                    if has_pinbar:
                        confirmations.append("🕯️ **Knot popytowy na świecy 4H (Pinbar)**")
                    if has_bullish_div:
                        confirmations.append("📈 **Bycza dywergencja RSI**")
                    if is_high_volatility:
                        confirmations.append("⚡️ **Podwyższona dynamika rynku (BBW)**")

                    conf_msg = ("\n**Potwierdzenia techniczne:**\n" + "\n".join(confirmations)) if confirmations else ""

                    if current_signal_type == "BUY_TREND":
                        task_title = f"TREND KUPNO: {original_symbol}"
                        msg = (
                            f"🟢 **STABILNE KUPNO W TRENDZIE WZROSTOWYM**\n\n"
                            f"🪙 **Moneta:** `{original_symbol}` | **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed}`\n"
                            f"📈 **Trend 1D:** `Wzrostowy 🟢`\n"
                            f"{conf_msg}\n\n"
                            f"👁 **Sprawdź sytuację na wykresie przed decyzją:**\n"
                            f"🔗 {tv_link}\n🔗 {t212_link}"
                        )
                    else:
                        task_title = f"⚡️ SZYBKIE ODBICIE: {original_symbol}"
                        msg = (
                            f"⚡️ **SZYBKA OKAZJA NA ODBICIE (SWING)**\n\n"
                            f"🪙 **Moneta:** `{original_symbol}` | **Cena:** `{display_price}`\n"
                            f"📊 **RSI 4H:** `{rsi_4h_closed}`\n"
                            f"📈 **Trend 1D:** `{'Wzrostowy 🟢' if is_uptrend_1d else 'Spadkowy/Korekta 🟡'}`\n"
                            f"{conf_msg}\n\n"
                            f"👁 **Oceń układ świec na wykresie:**\n"
                            f"🔗 {tv_link}\n🔗 {t212_link}"
                        )

                    pending_alerts.append((
                        task_title, msg, original_symbol, status_txt,
                        {'symbol': original_symbol, 'status': status_txt, 'price': display_price, 'rsi_4h': rsi_4h_closed}
                    ))

                cache[original_symbol] = {
                    "ts": ts_closed, 
                    "signal": signal_to_save,
                    "last_alert_time": current_epoch if should_alert else last_alert_time,
                    "last_sent_rsi": rsi_4h_closed if should_alert else last_sent_rsi,
                    "last_sent_price": close_closed if should_alert else last_sent_price
                }

            elapsed = round(time.time() - start_time, 2)
            health_summary = f"🩺 **Autodiagnostyka:** Przeanalizowano `{len(SYMBOLS)}` par w `{elapsed}s`."

            write_github_step_summary(digest_summary_rows, health_msg=health_summary)
            await process_telegram_commands_async(http_client, cache, digest_summary_rows)

            if pending_alerts:
                alert_coroutines = []
                for task_title, msg, _, _, meta in pending_alerts:
                    alert_coroutines.append(send_telegram_alert_async(http_client, msg))
                    alert_coroutines.append(add_to_tasks_async(tasks_service, task_title, msg.replace('**', '')))
                    rows_to_append_sheets.append([now_str, meta['symbol'], meta['status'], meta['price'], f"RSI: {meta['rsi_4h']}"])
                await asyncio.gather(*alert_coroutines)

            if sheet and rows_to_append_sheets:
                try:
                    await asyncio.to_thread(sheet.append_rows, rows_to_append_sheets)
                except Exception as e:
                    logging.error(f"Błąd Google Sheets: {e}")

            save_cache(cache)

        finally:
            await exchange.close()

    logging.info(f"Skaner zakończył działanie w: {elapsed}s.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"BŁĄD SKANERA: {e}", exc_info=True)
        raise e
