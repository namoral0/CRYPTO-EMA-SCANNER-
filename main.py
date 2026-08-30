import asyncio
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt.async_support as ccxt
import gspread
import pandas as pd
import requests

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

# Sztywne ryzyko £10 (zapobiega nadpisywaniu starymi zmiennymi z GitHub Secrets)
FIXED_RISK_GBP = 10.0

CORE_CRYPTO = ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
SYMBOLS = [
    "TAO/USD", "BTC/GBP", "ETH/GBP", "SOL/GBP", 
    "XRP/GBP", "RENDER/USD", "SUI/GBP", "LINK/GBP", "AAVE/GBP"
]
CACHE_FILE = "cache_krypto.json"

KRAKEN_SEMAPHORE = asyncio.Semaphore(3)


# --- 3. INICJALIZACJA USŁUG I ASYNCHRONICZNE I/O ---
def init_google_services():
    """Jednorazowa autoryzacja Google Tasks i Google Sheets bez ostrzeżeń w logach."""
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
                return json.load(f)
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {}

def save_cache(cache_data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache: {e}")

async def send_telegram_alert_async(msg):
    """Nieblokujące wysyłanie wiadomości na Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        await asyncio.to_thread(requests.post, url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Nie udało się wysłać wiadomości na Telegram: {e}")

async def add_to_tasks_async(tasks_service, title, notes):
    """Nieblokujące dodawanie zadania do Google Tasks."""
    if not tasks_service:
        return
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
    """Oblicza optymalną wielkość pozycji w GBP dla ustalonego ryzyka (£10)."""
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
    """Wyliczanie kompletnego zestawu wskaźników technicznych w pamięci RAM."""
    # 1D EMA 200
    df_1d['EMA_200'] = df_1d['close'].ewm(span=200, adjust=False).mean()

    # 4H EMA, BB, RSI, ATR, ADX
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

    # 15M RSI
    delta_15m = df_15m['close'].diff()
    gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
    loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
    df_15m['RSI'] = 100 - (100 / (1 + (gain_15m / loss_15m)))

    return df_1d, df_4h, df_15m


# --- 5. ASYNCHRONICZNE POBIERANIE DANYCH Z SEMAFOREM ---
async def fetch_ohlcv_retry_async(exchange, symbol, timeframe, limit=250, retries=3, delay=1.0):
    """Pobiera świece asynchronicznie z limitem połączeń."""
    async with KRAKEN_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:
                if attempt == retries - 1:
                    raise e
                await asyncio.sleep(delay)

async def fetch_ticker_safe(exchange, symbol):
    """Pobiera kurs rynkowy z użyciem semafora."""
    async with KRAKEN_SEMAPHORE:
        return await exchange.fetch_ticker(symbol)

async def fetch_symbol_data(exchange, symbol):
    """Pobiera dane 1D, 4H i 15M dla wybranego aktywa."""
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
    logging.info("Uruchamianie zoptymalizowanego skanera Krypto (Kraken CCXT Async + Safe I/O)...")
    
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    usd_gbp_rate = 0.78

    try:
        # 1. RÓWNOLEGŁE POBRANIE DANYCH BAZOWYCH Z KONTROLĄ RATE LIMITU
        btc_tasks = [
            fetch_ticker_safe(exchange, "GBP/USD"),
            fetch_ohlcv_retry_async(exchange, "BTC/GBP", "1d", 250),
            fetch_ohlcv_retry_async(exchange, "BTC/GBP", "4h", 300),
            fetch_ohlcv_retry_async(exchange, "BTC/USD", "1d", 250),
            fetch_ohlcv_retry_async(exchange, "BTC/USD", "4h", 300),
        ]
        
        ticker_gbp, btc_gbp_1d, btc_gbp_4h, btc_usd_1d, btc_usd_4h = await asyncio.gather(*btc_tasks)

        if ticker_gbp and ticker_gbp.get('last'):
            usd_gbp_rate = 1.0 / ticker_gbp['last']

        cols = ['ts', 'o', 'h', 'l', 'close', 'v']
        btc_cache = {
            "BTC/GBP": {"1d": pd.DataFrame(btc_gbp_1d, columns=cols), "4h": pd.DataFrame(btc_gbp_4h, columns=cols)},
            "BTC/USD": {"1d": pd.DataFrame(btc_usd_1d, columns=cols), "4h": pd.DataFrame(btc_usd_4h, columns=cols)},
        }

        # 2. RÓWNOLEGŁE POBRANIE WSZYSTKICH MONET
        results = await asyncio.gather(*[fetch_symbol_data(exchange, sym) for sym in SYMBOLS])

        digest_lines = []
        pending_alerts = []

        for symbol, df_1d, df_4h, df_15m in results:
            if df_1d is None or len(df_1d) < 30 or len(df_4h) < 100 or len(df_15m) < 20:
                continue

            is_core = symbol in CORE_CRYPTO
            quote_currency = symbol.split('/')[1]
            btc_symbol = f"BTC/{quote_currency}"

            df_1d, df_4h, df_15m = compute_indicators(df_1d, df_4h, df_15m)

            # BTC Macro Guard
            df_1d_btc = btc_cache[btc_symbol]["1d"]
            df_4h_btc = btc_cache[btc_symbol]["4h"]
            btc_ema_200_1d = df_1d_btc['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_btc_macro_bullish = bool(df_1d_btc['close'].iloc[-1] > btc_ema_200_1d)

            is_uptrend_1d = df_1d['close'].iloc[-1] > df_1d['EMA_200'].iloc[-1]
            ts_closed = int(df_4h['ts'].iloc[-2])
            adx_closed = df_4h['ADX'].iloc[-2]
            is_trending_4h = adx_closed > 25

            # Detekcja byczej dywergencji (Pivot Lows)
            has_bullish_div = check_bullish_divergence(df_4h)

            # Siła względna (RS vs BTC)
            is_strong_vs_btc_4h = True
            if symbol != btc_symbol:
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

            tp_target_raw = close_closed + (2.5 * atr_val) if is_uptrend_1d else bb_upper_closed

            if symbol.endswith("/USD"):
                price_gbp = close_closed * usd_gbp_rate
                display_price = f"£{price_gbp:.4f}" if price_gbp < 1 else f"£{price_gbp:.2f}"
                sl_calc_gbp = max(0, (close_closed - 1.5 * atr_val) * usd_gbp_rate)
                tp_calc_gbp = tp_target_raw * usd_gbp_rate
            else:
                price_gbp = close_closed
                display_price = f"£{close_closed:.4f}" if close_closed < 1 else f"£{close_closed:.2f}"
                sl_calc_gbp = max(0, close_closed - 1.5 * atr_val)
                tp_calc_gbp = tp_target_raw

            sl_str = f"£{sl_calc_gbp:.4f}" if sl_calc_gbp < 1 else f"£{sl_calc_gbp:.2f}"
            tp_str = f"£{tp_calc_gbp:.4f}" if tp_calc_gbp < 1 else f"£{tp_calc_gbp:.2f}"
            tp_str += " (Trailing ATR)" if is_uptrend_1d else " (BB Upper)"

            # Precyzyjne wyliczenie wielkości pozycji dla ryzyka FIXED_RISK_GBP (£10)
            position_gbp = calculate_position_size(price_gbp, sl_calc_gbp, FIXED_RISK_GBP)
            pos_size_str = f"£{position_gbp:.2f}" if position_gbp > 0 else "N/A"

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

            # Filtry bezpieczeństwa
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
            
            digest_lines.append(f"🪙 **{symbol}** — {display_price}\n  └ RSI: {rsi_4h_closed} | ADX: {round(adx_closed,1)} | Trend: {trend_txt} | Stan: {status_txt}{div_tag}\n")

            cached_data = cache.get(symbol, {})
            if not isinstance(cached_data, dict):
                cached_data = {}

            last_ts = cached_data.get("ts", 0)
            last_signal = cached_data.get("signal", "NONE")
            last_count = cached_data.get("count", 0)

            should_alert = False
            current_count = 0

            # Limit maksymalnie 2 alertów na świecę 4H
            if current_signal_type != "NONE":
                if last_signal == current_signal_type and last_ts == ts_closed:
                    current_count = last_count + 1
                    if current_count <= 2:
                        should_alert = True
                else:
                    current_count = 1
                    should_alert = True
            else:
                current_count = 0

            signal_to_save = current_signal_type if current_signal_type != "NONE" else (last_signal if last_ts == ts_closed else "NONE")

            if should_alert:
                t212_link = "[💼 Handluj na Trading 212](https://live.trading212.com/)"
                div_note = "\n📈 **Wykryto BYCZĄ DYWERGENCJĘ RSI (4H)!**\n" if has_bullish_div else ""
                risk_label = int(FIXED_RISK_GBP) if FIXED_RISK_GBP.is_integer() else FIXED_RISK_GBP

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
                        f"🛡 **Sugerowany Stop Loss:** `{sl_str}`\n🎯 **Sugerowany Take Profit:** `{tp_str}`\n\n"
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
                        f"🛡 **Sugerowany Stop Loss:** `{sl_str}`\n🎯 **Sugerowany Take Profit:** `{tp_str}`\n\n"
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
                "count": current_count if current_signal_type != "NONE" else 0
            }

        # 3. ZBIORCZA NIEBLOKUJĄCA WYSYŁKA ALERTÓW ORAZ BATCH DO GOOGLE SHEETS
        if pending_alerts:
            rows_to_append = []
            if len(pending_alerts) <= 3:
                for task_title, msg, _, _, meta in pending_alerts:
                    await send_telegram_alert_async(msg)
                    await add_to_tasks_async(tasks_service, task_title, msg.replace('**', ''))
                    rows_to_append.append([
                        now_str, meta['symbol'], meta['signal'],
                        meta['price'], f"RSI: {meta['rsi']}",
                        f"SL: {meta['sl']}", f"TP: {meta['tp']}"
                    ])
            else:
                lawina_title = f"🚨 LAWINOWA ZMIANA NA RYNKU: {len(pending_alerts)} ALERTÓW!"
                lawina_msg = f"⚠️ **ZMASOWANA ZMIANA STANU RYNKU ({len(pending_alerts)} MONET)**\n\n"
                for _, _, sym, stat, meta in pending_alerts:
                    lawina_msg += f"• **{sym}**: {stat}\n"
                    rows_to_append.append([
                        now_str, meta['symbol'], meta['signal'],
                        meta['price'], f"RSI: {meta['rsi']}",
                        f"SL: {meta['sl']}", f"TP: {meta['tp']}"
                    ])
                lawina_msg += "\n*Sprawdź szczegóły w aplikacji lub poczekaj na digest.*"
                
                await send_telegram_alert_async(lawina_msg)
                await add_to_tasks_async(tasks_service, lawina_title, lawina_msg.replace('**', ''))

            if sheet and rows_to_append:
                try:
                    await asyncio.to_thread(sheet.append_rows, rows_to_append)
                    logging.info(f"Zapisano {len(rows_to_append)} transakcji zbiorczo do Dziennika w Google Sheets.")
                except Exception as e:
                    logging.error(f"Nie udało się zapisać wierszy w Google Sheets: {e}")

        # CODZIENNY DIGEST AFTER 21:00
        if uk_now.hour >= 21 and cache.get("DIGEST_DATE") != today_str:
            if digest_lines:
                digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n" + "".join(digest_lines)
                await send_telegram_alert_async(digest_msg)
                await add_to_tasks_async(tasks_service, f"Codzienny Digest ({today_str})", digest_msg.replace('**', ''))
                cache["DIGEST_DATE"] = today_str

        save_cache(cache)

    finally:
        await exchange.close()

    elapsed = round(time.time() - start_time, 2)
    logging.info(f"Skaner Krypto zakończył działanie w czasie: {elapsed}s.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.critical(f"KRYTYCZNY BŁĄD SKANERA KRYPTO: {e}", exc_info=True)
        err_msg = f"🚨 **🔴 KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`"
        asyncio.run(send_telegram_alert_async(err_msg))
        raise e
