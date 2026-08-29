import datetime
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import gspread
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# --- 1. KONFIGURACJA LOGOWANIA ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# --- 2. ZMIENNE ŚRODOWISKOWE I STAŁE ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GOOGLE_TASKS_CREDENTIALS = os.getenv('GOOGLE_TASKS_CREDENTIALS')
GOOGLE_TASK_LIST_ID = os.getenv('GOOGLE_TASK_LIST_ID', '@default')

CORE_CRYPTO = ['BTC-GBP', 'ETH-GBP', 'SOL-GBP']
SATELLITE_CRYPTO = [
    'TAO-GBP', 'ONDO-GBP', 'XRP-GBP', 'RENDER-GBP', 
    'SUI-GBP', 'LINK-GBP', 'AAVE-GBP', 'FET-USD', 'ALGO-USD'
]
TICKERS = CORE_CRYPTO + SATELLITE_CRYPTO
CACHE_FILE = 'cache_krypto.json'


# --- 3. FUNKCJE POMOCNICZE (KOMUNIKACJA I STAN) ---
def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Nie można załadować pliku cache: {e}")
    return {}

def save_cache(cache_data):
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        logging.error(f"Nie udało się zapisać cache: {e}")

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={
                'chat_id': TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True,
            },
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        logging.error(f'Nie udało się wysłać wiadomości na Telegram: {e}')

def get_google_sheet():
    if not GOOGLE_TASKS_CREDENTIALS or not SPREADSHEET_ID:
        return None
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_TASKS_CREDENTIALS),
            scopes=['https://www.googleapis.com/auth/spreadsheets'],
        )
        return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
    except Exception as e:
        logging.error(f'Błąd autoryzacji Google Sheets: {e}')
        return None

def add_to_tasks(title, notes):
    if not GOOGLE_TASKS_CREDENTIALS:
        return
    try:
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_TASKS_CREDENTIALS),
            scopes=['https://www.googleapis.com/auth/tasks'],
        )
        service = build('tasks', 'v1', credentials=creds)
        service.tasks().insert(
            tasklist=GOOGLE_TASK_LIST_ID, 
            body={'title': title, 'notes': notes}
        ).execute()
        logging.info(f"Dodano zadanie do Google Tasks: {title}")
    except Exception as e:
        logging.error(f'Błąd podczas dodawania zadania do Google Tasks: {e}')

def fetch_safe_history(ticker, period, interval):
    """Pobiera historię z yfinance, a przy braku danych GBP wykonuje fallback na USD."""
    try:
        stock = yf.Ticker(ticker)
        df = stock.history(period=period, interval=interval)
        
        if df.empty and ticker.endswith('-GBP'):
            fallback_ticker = ticker.replace('-GBP', '-USD')
            logging.warning(f"Brak danych dla {ticker} ({interval}). Próba pobrania {fallback_ticker}...")
            stock = yf.Ticker(fallback_ticker)
            df = stock.history(period=period, interval=interval)
            
        return df, stock
    except Exception as e:
        logging.error(f"Błąd pobierania danych dla {ticker}: {e}")
        return pd.DataFrame(), None


# --- 4. FUNKCJE ANALITYCZNE ---
def calculate_rsi(series, window=14):
    clean = series.dropna()
    if len(clean) < window:
        return 50.0
    delta = clean.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean().replace(0, 1e-10)
    rs = avg_gain / avg_loss
    return round((100 - (100 / (1 + rs))).iloc[-1], 2)

def detect_rsi_divergence(df_4h, window=25):
    if len(df_4h) < window or 'RSI' not in df_4h.columns:
        return False

    closes = df_4h['Close']
    rsis = df_4h['RSI']

    recent_closes = closes.iloc[-5:]
    recent_min_price = recent_closes.min()
    recent_min_idx = recent_closes.idxmin()
    recent_rsi = rsis.loc[recent_min_idx]

    older_closes = closes.iloc[-window:-5]
    if len(older_closes) == 0:
        return False
        
    older_rsis = rsis.iloc[-window:-5]
    older_min_price = older_closes.min()
    older_min_idx = older_closes.idxmin()
    older_rsi = older_rsis.loc[older_min_idx]

    if recent_min_price < older_min_price and recent_rsi > (older_rsi + 2.0):
        return True
    return False

def get_btc_performance():
    try:
        df_btc, _ = fetch_safe_history('BTC-GBP', period='5d', interval='1d')
        if len(df_btc) >= 2:
            return ((df_btc['Close'].iloc[-1] - df_btc['Close'].iloc[-2]) / df_btc['Close'].iloc[-2]) * 100
    except Exception as e:
        logging.error(f'Problem z pobraniem referencyjnego BTC: {e}')
    return 0.0


# --- 5. LOGIKA BIZNESOWA DLA POJEDYNCZEGO TICKERA ---
def analyze_ticker(ticker, is_core, btc_perf_24h):
    df_1d, stock = fetch_safe_history(ticker, period='1y', interval='1d')
    if df_1d.empty:
        raise ValueError(f"Brak danych dziennych dla {ticker}.")

    df_1d = df_1d.ffill()
    clean_1d_close = df_1d['Close'].dropna()
    if len(clean_1d_close) < 50:
        raise ValueError(f"Zbyt mało danych dziennych dla {ticker} (wymagane min. 50).")

    actual_ticker = stock.ticker if stock else ticker
    symbol_native = '£' if actual_ticker.endswith('-GBP') else '$'
    crypto_icon = '🪙' if is_core else '🛰️'
    clean_name = actual_ticker.replace('-', '/')
    ticker_link = f'[{clean_name}](https://finance.yahoo.com/quote/{actual_ticker})'

    try:
        price_native = float(stock.fast_info.get('lastPrice', clean_1d_close.iloc[-1]))
    except Exception:
        price_native = float(clean_1d_close.iloc[-1])

    price_display = f'{symbol_native}{price_native:.4f}' if price_native < 1 else f'{symbol_native}{price_native:.2f}'

    high_52w = df_1d['High'].dropna().max()
    drawdown_pct = round(((price_native - high_52w) / high_52w) * 100, 1) if high_52w > 0 else 0

    ema200_1d = clean_1d_close.ewm(span=200, adjust=False).mean().iloc[-1]
    is_uptrend_1d = price_native > ema200_1d
    rsi_1d = calculate_rsi(clean_1d_close)

    # Analiza zmienności (BBW + ATR)
    df_1d['BB_mid'] = clean_1d_close.rolling(window=20).mean()
    df_1d['BB_std'] = clean_1d_close.rolling(window=20).std()
    df_1d['BBW'] = (df_1d['BB_mid'] + (df_1d['BB_std'] * 2) - (df_1d['BB_mid'] - (df_1d['BB_std'] * 2))) / df_1d['BB_mid']
    
    bbw_valid = df_1d['BBW'].dropna()
    bbw_avg = bbw_valid.rolling(window=20).mean().iloc[-1] if len(bbw_valid) >= 20 else 1.0
    bbw_current = bbw_valid.iloc[-1] if len(bbw_valid) > 0 else 1.0
    vol_mult = (bbw_current / bbw_avg) if (not pd.isna(bbw_avg) and bbw_avg > 0) else 1.0

    true_range = pd.concat([
        df_1d['High'] - df_1d['Low'],
        (df_1d['High'] - df_1d['Close'].shift()).abs(),
        (df_1d['Low'] - df_1d['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    
    df_1d['ATR'] = true_range.rolling(window=14).mean()
    df_1d['ATR_MA'] = df_1d['ATR'].rolling(window=20).mean()
    
    atr_curr = df_1d['ATR'].iloc[-1]
    atr_ma_curr = df_1d['ATR_MA'].iloc[-1]
    is_anomaly = (atr_curr > (atr_ma_curr * 2.5)) if (not pd.isna(atr_ma_curr) and atr_ma_curr > 0) else False

    # Wsparcie tygodniowe 1W
    bb_1w_val = None
    bb_1w_msg = '📐 **Wsparcie 1W:** `Brak danych`'
    try:
        df_1w, _ = fetch_safe_history(actual_ticker, period='2y', interval='1wk')
        if not df_1w.empty:
            clean_1w = df_1w['Close'].dropna()
            if len(clean_1w) >= 21:
                closed_1w = clean_1w.iloc[:-1]
                bb_1w_val = (closed_1w.rolling(20).mean().iloc[-1] - (closed_1w.rolling(20).std().iloc[-1] * 2))
                if not pd.isna(bb_1w_val):
                    dist = round(((bb_1w_val - price_native) / price_native) * 100, 1) if price_native > 0 else 0
                    val_fmt = f'{bb_1w_val:.4f}' if bb_1w_val < 1 else f'{bb_1w_val:.2f}'
                    bb_1w_msg = f'📐 **Wsparcie 1W (BB):** `{symbol_native}{val_fmt}`\n📉 **Dystans do 1W:** `{dist}%`'
    except Exception as e:
        logging.debug(f'Brak dostępu do danych 1W dla {actual_ticker}: {e}')

    near_supports = []
    if not pd.isna(ema200_1d) and ema200_1d > 0 and abs(price_native - ema200_1d) / price_native <= 0.025:
        near_supports.append('EMA 200 1D')
    if bb_1w_val is not None and not pd.isna(bb_1w_val) and bb_1w_val > 0 and abs(price_native - bb_1w_val) / price_native <= 0.025:
        near_supports.append('1W BB Lower')
    low_90d = df_1d['Low'].tail(90).min()
    if not pd.isna(low_90d) and low_90d > 0 and abs(price_native - low_90d) / price_native <= 0.02:
        near_supports.append('Dołek 3M')

    is_near_support = len(near_supports) > 0
    support_txt = ', '.join(near_supports) if is_near_support else 'Brak'

    # Siła względna (RS vs BTC)
    is_rs_vs_btc = False
    if not is_core and len(clean_1d_close) >= 2:
        token_perf_24h = ((clean_1d_close.iloc[-1] - clean_1d_close.iloc[-2]) / clean_1d_close.iloc[-2]) * 100
        if token_perf_24h > (btc_perf_24h + 1.5):
            is_rs_vs_btc = True

    # Analiza 1H / 4H
    df_1h, _ = fetch_safe_history(actual_ticker, period='1mo', interval='1h')
    rsi_4h = 50.0
    is_rsi_div = False
    is_pinbar = False

    if not df_1h.empty and len(df_1h) > 50:
        df_1h = df_1h.ffill()
        df_4h_df = df_1h.resample('4h').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'
        }).dropna()

        # Odrzucamy trwającą, niepełną świecę 4H
        if len(df_4h_df) > 1:
            df_4h_df = df_4h_df.iloc[:-1]

        if len(df_4h_df) >= 14:
            clean_4h = df_4h_df['Close'].dropna()
            delta_4h = clean_4h.diff()
            gain_4h = delta_4h.where(delta_4h > 0, 0)
            loss_4h = -delta_4h.where(delta_4h < 0, 0)
            avg_gain_4h = gain_4h.ewm(alpha=1 / 14, adjust=False).mean()
            avg_loss_4h = loss_4h.ewm(alpha=1 / 14, adjust=False).mean().replace(0, 1e-10)
            rs_4h = avg_gain_4h / avg_loss_4h
            df_4h_df['RSI'] = round(100 - (100 / (1 + rs_4h)), 2)
            
            rsi_4h = df_4h_df['RSI'].iloc[-1]
            is_rsi_div = detect_rsi_divergence(df_4h_df)

        last_open, last_high, last_low = df_1h['Open'].iloc[-1], df_1h['High'].iloc[-1], df_1h['Low'].iloc[-1]
        candle_range = last_high - last_low
        if candle_range > 0:
            lower_wick = min(last_open, price_native) - last_low
            if (lower_wick / candle_range) >= 0.40:
                is_pinbar = True

    # Sygnały
    base_buy_thr = 36 if is_core else 28
    min_floor = 30 if is_core else 22

    buy_thr_4h = max(min_floor, base_buy_thr - 4) if vol_mult > 1.3 else base_buy_thr
    sell_thr_4h = max(70, 75 if vol_mult > 1.3 else 68) if is_uptrend_1d else 50

    if is_anomaly:
        is_buy, is_sell = False, False
    else:
        is_buy = (rsi_4h < buy_thr_4h) or (rsi_4h < (buy_thr_4h + 5) and is_rsi_div)
        is_sell = rsi_4h >= sell_thr_4h

    signal = 'NONE'
    if is_sell: signal = 'WYKUPIENIE'
    elif is_buy: signal = 'WYPRZEDANIE_ZIELONE' if (is_uptrend_1d or is_core) else 'WYPRZEDANIE_ZOLTE'

    status_tag = []
    if is_rsi_div: status_tag.append('🎯 DYWERGENCJA')
    if is_near_support: status_tag.append('🛡️ WSPARCIE')
    if is_pinbar: status_tag.append('🕯️ PINBAR')
    if is_rs_vs_btc: status_tag.append('🚀 RS vs BTC')
    tag_str = f" [{', '.join(status_tag)}]" if status_tag else ''

    status_txt = {
        'NONE': '⚪ Neutralny',
        'WYPRZEDANIE_ZIELONE': f'🟢 MEGA OKAZJA{tag_str}',
        'WYPRZEDANIE_ZOLTE': f'🟡 Dołek 4H{tag_str}',
        'WYKUPIENIE': '🟠 Take Profit / 🔴 Ewakuacja',
    }.get(signal, '⚪ Neutralny')

    trend_txt = '↗️' if is_uptrend_1d else '↘️'
    digest_line = f'{crypto_icon} {ticker_link} — {price_display}\n  └ RSI 4h: {rsi_4h} | Trend: {trend_txt} | Stan: {status_txt}\n'

    return {
        'ticker': actual_ticker, 'clean_name': clean_name, 'crypto_icon': crypto_icon,
        'ticker_link': ticker_link, 'price_display': price_display, 'drawdown_pct': drawdown_pct,
        'rsi_1d': rsi_1d, 'rsi_4h': rsi_4h, 'is_uptrend_1d': is_uptrend_1d,
        'bb_1w_msg': bb_1w_msg, 'support_txt': support_txt, 'is_rsi_div': is_rsi_div,
        'is_near_support': is_near_support, 'is_pinbar': is_pinbar, 'is_rs_vs_btc': is_rs_vs_btc,
        'buy_thr_4h': buy_thr_4h, 'sell_thr_4h': sell_thr_4h, 'signal': signal,
        'digest_line': digest_line, 'is_core': is_core
    }

def generate_alert_content(data):
    ranga_label = 'Core 🏛️' if data['is_core'] else 'Satelita 🛰️'
    
    confirmations = []
    if data['is_rsi_div']: confirmations.append('🎯 **Bycza Dywergencja RSI (4H)**')
    if data['is_near_support']: confirmations.append(f"🛡️ **Strefa Wsparcia:** {data['support_txt']}")
    if data['is_pinbar']: confirmations.append('🕯️ **Knot Popytowy (Pinbar 4H)**')
    if data['is_rs_vs_btc']: confirmations.append('🚀 **Siła Względna:** Wyprzedza BTC')
    conf_msg = '\n' + '\n'.join(confirmations) if confirmations else ''

    if data['signal'] == 'WYKUPIENIE':
        if data['is_uptrend_1d']:
            title = f"TAKE PROFIT: {data['clean_name']} (RSI {data['rsi_4h']})"
            body = (
                f"🟠 **REALIZACJA ZYSKU (KRYPTO)!**\n\n{data['crypto_icon']} **Aktywo:** {data['ticker_link']}\n"
                f"💰 **Cena:** {data['price_display']}\n📉 **Od szczytu (52W):** {data['drawdown_pct']}%\n"
                f"💪 **Ranga:** {ranga_label}\n📊 **RSI 4H:** {data['rsi_4h']} (Próg: {data['sell_thr_4h']})\n"
                f"📊 **RSI 1d:** {data['rsi_1d']}"
            )
        else:
            title = f"🔴 KRYTYCZNE: SPRZEDAJ {data['clean_name']}!"
            body = (
                f"🚨 **🔴 EWAKUACJA! TREND SPADKOWY! NATYCHMIAST SPRZEDAJ {data['clean_name'].upper()}!**\n"
                f"🚨 **🔴 WYMAGANA PILNA OPERACJA NA RYNKU KRYPTO!**\n\n"
                f"🔴 **AKTYWO:** {data['ticker_link'].upper()}\n🔴 **CENA:** {data['price_display']}\n"
                f"🔴 **RSI 4H:** {data['rsi_4h']} (ODBICIE OD OPORU)"
            )
    elif data['signal'] == 'WYPRZEDANIE_ZIELONE':
        title = f"MEGA OKAZJA KRYPTO: KUP {data['clean_name']}"
        body = (
            f"🟢 **MEGA OKAZJA / HOSSA (DOŁEK KRYPTO)**\n\n{data['crypto_icon']} **Aktywo:** {data['ticker_link']}\n"
            f"💰 **Cena:** {data['price_display']}\n📉 **Od szczytu (52W):** {data['drawdown_pct']}%\n"
            f"💪 **Ranga:** {ranga_label}\n📊 **RSI 4h:** {data['rsi_4h']} (Próg: {data['buy_thr_4h']})\n"
            f"📊 **RSI 1d:** {data['rsi_1d']}\n{data['bb_1w_msg']}{conf_msg}"
        )
    else:
        title = f"SWING KRYPTO: OBSERWUJ {data['clean_name']}"
        body = (
            f"🟡 **OKAZJA SWING / {ranga_label.upper()} (DOŁEK 4H)**\n\n{data['crypto_icon']} **Aktywo:** {data['ticker_link']}\n"
            f"💰 **Cena:** {data['price_display']}\n📉 **Od szczytu (52W):** {data['drawdown_pct']}%\n"
            f"💪 **Ranga:** {ranga_label}\n📊 **RSI 4h:** {data['rsi_4h']} (Próg: {data['buy_thr_4h']})\n"
            f"📊 **RSI 1d:** {data['rsi_1d']}\n{data['bb_1w_msg']}{conf_msg}"
        )
        
    return title, body


# --- 6. GŁÓWNA PĘTLA APLIKACJI ---
def process_single_ticker(ticker, btc_perf_24h, cache, sheet, today_str, now_str):
    try:
        is_core = ticker in CORE_CRYPTO
        data = analyze_ticker(ticker, is_core, btc_perf_24h)
        
        actual_ticker = data['ticker']
        signal = data['signal']
        last_sig = cache.get(actual_ticker, {}).get('signal', 'NONE')
        last_date = cache.get(actual_ticker, {}).get('date', '')

        if signal != 'NONE' and (last_date != today_str or last_sig != signal):
            title, body = generate_alert_content(data)
            
            send_telegram(body)
            add_to_tasks(title, body.replace('**', '').replace('`', ''))

            if sheet:
                try:
                    sheet.append_row([now_str, actual_ticker, data['price_display'], data['rsi_4h'], data['rsi_1d'], signal])
                except Exception as e:
                    logging.error(f"Błąd zapisu do Google Sheets dla {actual_ticker}: {e}")

            cache[actual_ticker] = {'signal': signal, 'date': today_str}
            
        return data['digest_line']
        
    except Exception as e:
        logging.error(f"Błąd przetwarzania krypto dla {ticker}: {str(e)}")
        return None


def main():
    logging.info("Uruchamianie skanera Krypto...")
    uk_now = datetime.datetime.now(ZoneInfo('Europe/London'))
    today_str = uk_now.strftime('%Y-%m-%d')
    now_str = uk_now.strftime('%Y-%m-%d %H:%M')

    cache = load_cache()
    sheet = get_google_sheet()
    btc_perf_24h = get_btc_performance()
    digest_lines = []

    for ticker in TICKERS:
        time.sleep(1.2)
        digest_line = process_single_ticker(ticker, btc_perf_24h, cache, sheet, today_str, now_str)
        if digest_line:
            digest_lines.append(digest_line)
            
        save_cache(cache)

    # Codzienne podsumowanie po 21:00 UK
    if uk_now.hour >= 21 and cache.get('DIGEST_DATE') != today_str:
        if digest_lines:
            digest_msg = '📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n' + ''.join(digest_lines)
            send_telegram(digest_msg)
            add_to_tasks(f'Podsumowanie Krypto ({today_str})', digest_msg.replace('**', '').replace('`', ''))
            
            cache['DIGEST_DATE'] = today_str
            save_cache(cache)
            logging.info("Wysłano codzienne podsumowanie.")

    logging.info("Skaner zakończył działanie.")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logging.critical(f"🚨 KRYTYCZNY BŁĄD SKANERA KRYPTO: {e}", exc_info=True)
        err_msg = f'🚨 **🔴 KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`'
        send_telegram(err_msg)
        add_to_tasks('KRYTYCZNY BŁĄD SKANERA KRYPTO', err_msg)
        raise e
