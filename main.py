import os
import datetime
import json
import requests
import time
import yfinance as yf
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from zoneinfo import ZoneInfo

# --- KONFIGURACJA ZMIENNYCH ŚRODOWISKOWYCH ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GOOGLE_TASKS_CREDENTIALS = os.getenv('GOOGLE_TASKS_CREDENTIALS')

CORE_STOCKS = ['ASML.AS', 'V']
HIGH_BETA_STOCKS = ['RKLB', 'IONQ']
TICKERS = ['ASML.AS', 'V', 'BESI.AS', 'RKLB', 'SMHN.DE', 'IONQ', 'OVH.PA', 'IFX.DE', 'STMPA.PA']
CACHE_FILE = "cache_akcje.json"

def get_exchange_rate(ticker_symbol):
    try:
        fx = yf.Ticker(ticker_symbol).history(period="1d")
        return fx['Close'].iloc[-1]
    except Exception:
        return 0.85 if 'EUR' in ticker_symbol else 0.78

def calculate_rsi(series, window=14):
    clean = series.dropna()
    if len(clean) < window: return 50.0
    delta = clean.diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta.where(delta < 0, 0))
    avg_gain = gain.ewm(alpha=1/window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/window, adjust=False).mean().replace(0, 1e-10)
    rs = avg_gain / avg_loss
    return round((100 - (100 / (1 + rs))).iloc[-1], 2)

def send_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}, 
                timeout=10
            )
        except Exception: pass

def get_google_sheet():
    if not GOOGLE_TASKS_CREDENTIALS or not SPREADSHEET_ID: return None
    try:
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_TASKS_CREDENTIALS), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
    except Exception: return None

def add_to_tasks(title, notes):
    if not GOOGLE_TASKS_CREDENTIALS: return
    try:
        creds = Credentials.from_service_account_info(json.loads(GOOGLE_TASKS_CREDENTIALS), scopes=["https://www.googleapis.com/auth/tasks"])
        build('tasks', 'v1', credentials=creds).tasks().insert(tasklist='@default', body={'title': title, 'notes': notes}).execute()
    except Exception: pass

def main():
    uk_now = datetime.datetime.now(ZoneInfo("Europe/London"))
    if uk_now.weekday() >= 5: 
        print("Weekend. Giełda akcji jest zamknięta.")
        return

    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")
    
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: cache = json.load(f)
        except Exception: pass

    sheet = get_google_sheet()
    
    # Pobranie kursów walut do przeliczenia na GBP
    rate_eur_gbp = get_exchange_rate('EURGBP=X')
    rate_usd_gbp = get_exchange_rate('GBP=X') 
    
    benchmark_df = None
    try:
        benchmark_df = yf.Ticker("^GSPC").history(period="1y", interval="1d")
    except Exception: pass
    
    digest_lines = []

    for ticker in TICKERS:
        try:
            time.sleep(1.2)
            is_core = ticker in CORE_STOCKS
            is_european = ticker.endswith(('.AS', '.PA', '.DE', '.MC'))
            
            stock_icon = "🏛️"
            ticker_link = f"[{ticker}](https://finance.yahoo.com/quote/{ticker})"
            
            stock = yf.Ticker(ticker)
            
            # =========================================================================
            # KROK 1: ANALIZA MAKRO (1D) — TŁO RYNKOWE, TREND I RYZYKO
            # =========================================================================
            df_1d = stock.history(period="1y", interval="1d").ffill()
            clean_1d_close = df_1d['Close'].dropna()
            if len(clean_1d_close) < 50: continue
            
            rate = rate_eur_gbp if is_european else rate_usd_gbp
            
            # Pobranie ceny rynkowej live i przeliczenie na GBP
            try:
                price_native = float(stock.fast_info.get('lastPrice', clean_1d_close.iloc[-1]))
            except Exception:
                price_native = clean_1d_close.iloc[-1]
                
            price_gbp = price_native * rate
            price_display = f"£{price_gbp:.2f}"
            
            high_52w = df_1d['High'].dropna().max()
            drawdown_pct = round(((price_native - high_52w) / high_52w) * 100, 1) if high_52w > 0 else 0
            
            ema200_1d = clean_1d_close.ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = price_native > ema200_1d
            
            rsi_1d = calculate_rsi(clean_1d_close)
            
            df_1d['BB_mid'] = clean_1d_close.rolling(window=20).mean()
            df_1d['BB_std'] = clean_1d_close.rolling(window=20).std()
            df_1d['BBW'] = (df_1d['BB_mid'] + (df_1d['BB_std'] * 2) - (df_1d['BB_mid'] - (df_1d['BB_std'] * 2))) / df_1d['BB_mid']
            bbw_avg = df_1d['BBW'].dropna().rolling(window=20).mean().iloc[-1]
            bbw_current = df_1d['BBW'].dropna().iloc[-1]
            vol_mult = (bbw_current / bbw_avg) if (not pd.isna(bbw_avg) and bbw_avg > 0) else 1.0

            true_range = pd.concat([df_1d['High'] - df_1d['Low'], (df_1d['High'] - df_1d['Close'].shift()).abs(), (df_1d['Low'] - df_1d['Close'].shift()).abs()], axis=1).max(axis=1)
            df_1d['ATR'] = true_range.rolling(window=14).mean()
            df_1d['ATR_MA'] = df_1d['ATR'].rolling(window=20).mean()
            atr_current = df_1d['ATR'].iloc[-1]
            atr_ma_current = df_1d['ATR_MA'].iloc[-1]
            is_anomaly = atr_current > (atr_ma_current * 2.5) if not pd.isna(atr_ma_current) and atr_ma_current > 0 else False

            is_strong_vs_market = True
            if benchmark_df is not None:
                stock_close = clean_1d_close.copy()
                if hasattr(stock_close.index, 'tz') and stock_close.index.tz is not None:
                    stock_close.index = stock_close.index.tz_localize(None)
                stock_close.index = pd.to_datetime(stock_close.index).normalize()
                
                bench_close = benchmark_df['Close'].dropna().copy()
                if hasattr(bench_close.index, 'tz') and bench_close.index.tz is not None:
                    bench_close.index = bench_close.index.tz_localize(None)
                bench_close.index = pd.to_datetime(bench_close.index).normalize()
                
                combined = pd.DataFrame({'stock': stock_close, 'bench': bench_close}).dropna()
                if len(combined) > 20:
                    rs_series = combined['stock'] / (combined['bench'] + 1e-10)
                    is_strong_vs_market = rs_series.iloc[-1] >= rs_series.ewm(span=20, adjust=False).mean().iloc[-1]

            bb_1w_msg = "📐 **Wsparcie 1W:** `Brak danych`"
            try:
                df_1w = stock.history(period="2y", interval="1wk").ffill()
                clean_1w = df_1w['Close'].dropna()
                if len(clean_1w) >= 21:
                    closed_1w = clean_1w.iloc[:-1]
                    val_native = closed_1w.rolling(20).mean().iloc[-1] - (closed_1w.rolling(20).std().iloc[-1] * 2)
                    if not pd.isna(val_native):
                        val_gbp = val_native * rate
                        dist = round(((val_native - price_native) / price_native) * 100, 1) if price_native > 0 else 0
                        bb_1w_msg = f"📐 **Wsparcie 1W (BB):** `£{val_gbp:.2f}`\n📉 **Dystans do 1W:** `{dist}%`"
            except: pass

            if is_core:
                base_buy_thr = 38
                min_floor = 32
            elif ticker in HIGH_BETA_STOCKS:
                base_buy_thr = 26
                min_floor = 22
            else:
                base_buy_thr = 33
                min_floor = 28

            buy_thr_4h = max(min_floor, base_buy_thr - 4) if vol_mult > 1.3 else base_buy_thr
            sell_thr_4h = min(72, 65 + 4) if vol_mult > 1.3 else 65
            if is_uptrend_1d: sell_thr_4h = max(sell_thr_4h, 75)

            # =========================================================================
            # KROK 2: ANALIZA MIKRO (4H) — PRECYZYJNY SPUST TRANSAKCYJNY (TRIGGER)
            # =========================================================================
            df_1h = stock.history(period="3mo", interval="1h").ffill()
            if len(df_1h) > 50:
                if hasattr(df_1h.index, 'tz') and df_1h.index.tz is not None:
                    df_1h.index = df_1h.index.tz_convert("Europe/London").tz_localize(None)
                else:
                    df_1h.index = pd.to_datetime(df_1h.index)
                    if hasattr(df_1h.index, 'tz') and df_1h.index.tz is not None:
                        df_1h.index = df_1h.index.tz_localize(None)
                
                if is_european:
                    rth_start, rth_end = datetime.time(8, 0), datetime.time(16, 30)
                    df_rth = df_1h.between_time("08:00", "16:30").copy()
                    offset = pd.Timedelta(hours=0)
                else:
                    rth_start, rth_end = datetime.time(14, 30), datetime.time(21, 0)
                    df_rth = df_1h.between_time("14:30", "21:00").copy()
                    offset = pd.Timedelta(hours=2, minutes=30) 

                current_time_val = uk_now.time()
                if rth_start <= current_time_val <= rth_end and len(df_rth) > 0:
                    df_rth.iloc[-1, df_rth.columns.get_loc('Close')] = price_native

                if len(df_rth) > 0:
                    df_4h_close = df_rth['Close'].dropna().resample('4h', origin='start', offset=offset).last().dropna()
                    rsi_4h = calculate_rsi(df_4h_close)
                else:
                    rsi_4h = 50.0
            else:
                rsi_4h = 50.0

            # =========================================================================
            # KROK 3: KWALIFIKACJA SYGNAŁU (SITOWANIE 1D + 4H)
            # =========================================================================
            if is_anomaly:
                is_buy = False
                is_sell = False
            else:
                is_buy = (rsi_4h < buy_thr_4h)
                is_sell = (rsi_4h >= sell_thr_4h)

            signal = "NONE"
            if is_sell:
                signal = "WYKUPIENIE"
            elif is_buy:
                daily_confirm_thr = buy_thr_4h + (4 if is_core else 3)
                if (rsi_1d < daily_confirm_thr) or (is_strong_vs_market and vol_mult > 1.4) or is_core:
                    signal = "WYPRZEDANIE_ZIELONE"
                else:
                    signal = "WYPRZEDANIE_ZOLTE"

            status_txt = {
                "NONE": "⚪ Neutralny", 
                "WYPRZEDANIE_ZIELONE": "🟢 MEGA OKAZJA", 
                "WYPRZEDANIE_ZOLTE": "🟡 Dołek 4H", 
                "WYKUPIENIE": "🟠 Take Profit"
            }.get(signal, "⚪ Neutralny")
            
            trend_txt = "↗️" if is_uptrend_1d else "↘️"
            digest_lines.append(f"{stock_icon} {ticker_link} — {price_display}\n  └ RSI 4h: {rsi_4h} | Trend: {trend_txt} | Stan: {status_txt}\n")

            # =========================================================================
            # KROK 4: WYSYŁANIE ALERTÓW (Z BLOKADĄ EUROPY PO 16:30 UK)
            # =========================================================================
            last_sig = cache.get(ticker, {}).get("signal", "NONE")
            last_date = cache.get(ticker, {}).get("date", "")

            if signal != "NONE" and (last_date != today_str or last_sig != signal):
                
                is_after_eu_close = is_european and (uk_now.hour > 16 or (uk_now.hour == 16 and uk_now.minute >= 30))
                
                if not is_after_eu_close:
                    ranga_label = "Core 🏛️" if is_core else "Satelita 🛰️"
                    
                    if signal == "WYKUPIENIE":
                        if is_uptrend_1d:
                            title = f"TAKE PROFIT: {ticker} (RSI {rsi_4h})"
                            body = f"🟠 **REALIZACJA ZYSKU! REBALANCING!**\n\n{stock_icon} **Spółka:** {ticker_link}\n💰 **Cena:** {price_display}\n📉 **Od szczytu (52W):** {drawdown_pct}%\n💪 **Ranga:** {ranga_label}\n📊 **RSI 4H:** {rsi_4h} (Próg: {sell_thr_4h})\n📊 **RSI 1d:** {rsi_1d}"
                        else:
                            title = f"🔴 PILNE: SPRZEDAJ! {ticker.upper()}"
                            body = f"🔴 **EWAKUACJA! SPADEK TRENDU! NATYCHMIAST SPRZEDAJ!**\n\n🔴 **SPÓŁKA: {ticker_link}**\n🔴 **CENA: {price_display}**\n🔴 **RSI 4H: {rsi_4h}**"
                    elif signal == "WYPRZEDANIE_ZIELONE":
                        title = f"MEGA OKAZJA: KUP {ticker}"
                        body = f"🟢 **MEGA OKAZJA / HOSSA (KUPNO DOŁKA)**\n\n{stock_icon} **Spółka:** {ticker_link}\n💰 **Cena:** {price_display}\n📉 **Od szczytu (52W):** {drawdown_pct}%\n💪 **Ranga:** {ranga_label}\n📊 **RSI 4h:** {rsi_4h} (Próg: {buy_thr_4h})\n📊 **RSI 1d:** {rsi_1d}\n{bb_1w_msg}"
                    else:
                        title = f"SWING: OBSERWUJ {ticker}"
                        body = f"🟡 **OKAZJA SWING / {ranga_label.upper()} (DOŁEK 4H)**\n\n{stock_icon} **Spółka:** {ticker_link}\n💰 **Cena:** {price_display}\n📉 **Od szczytu (52W):** {drawdown_pct}%\n💪 **Ranga:** {ranga_label}\n📊 **RSI 4h:** {rsi_4h} (Próg: {buy_thr_4h})\n📊 **RSI 1d:** {rsi_1d}\n{bb_1w_msg}"

                    send_telegram(body)
                    add_to_tasks(title, body.replace('**', '').replace('`', ''))
                    
                    if sheet: 
                        try: sheet.append_row([now_str, ticker, price_display, rsi_4h, rsi_1d, signal])
                        except: pass
                        
                    cache[ticker] = {"signal": signal, "date": today_str}

        except Exception as e:
            print(f"Błąd {ticker}: {e}")

    # Podsumowanie dzienne (po 21:00 UK)
    if uk_now.hour >= 21 and cache.get("DIGEST_DATE") != today_str:
        if digest_lines:
            digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (AKCJE)**\n\n" + "".join(digest_lines)
            send_telegram(digest_msg)
            add_to_tasks(f"Podsumowanie Akcji ({today_str})", digest_msg.replace('**', '').replace('`', ''))
            cache["DIGEST_DATE"] = today_str

    with open(CACHE_FILE, "w") as f: json.dump(cache, f)

if __name__ == "__main__":
    main()
            
