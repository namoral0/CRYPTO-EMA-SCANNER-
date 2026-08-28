import datetime
import json
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

# --- KONFIGURACJA ZMIENNYCH ŚRODOWISKOWYCH ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')
GOOGLE_TASKS_CREDENTIALS = os.getenv('GOOGLE_TASKS_CREDENTIALS')

CORE_CRYPTO = ['BTC-GBP', 'ETH-GBP', 'SOL-GBP']
SATELLITE_CRYPTO = ['FET-USD', 'ALGO-USD']
TICKERS = CORE_CRYPTO + SATELLITE_CRYPTO
CACHE_FILE = 'cache_krypto.json'


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
  """Wykrywa byczą dywergencję: niższy dołek ceny, ale wyższy dołek RSI w oknie 4H."""
  if len(df_4h) < window or 'RSI' not in df_4h.columns:
    return False

  closes = df_4h['Close']
  rsis = df_4h['RSI']

  recent_closes = closes.iloc[-5:]
  recent_min_price = recent_closes.min()
  recent_min_idx = recent_closes.idxmin()
  recent_rsi = rsis.loc[recent_min_idx]

  older_closes = closes.iloc[-window:-5]
  older_rsis = rsis.iloc[-window:-5]

  if len(older_closes) == 0:
    return False

  older_min_price = older_closes.min()
  older_min_idx = older_closes.idxmin()
  older_rsi = older_rsis.loc[older_min_idx]

  # Bycza Dywergencja: Nowy dołek cenowy, ale RSI wyższe o min. 2.0 pkt
  if recent_min_price < older_min_price and recent_rsi > (older_rsi + 2.0):
    return True
  return False


def send_telegram(message):
  if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
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
    except Exception as e:
      print(f'❌ Nie udało się wysłać wiadomości na Telegram: {e}')


def get_google_sheet():
  if not GOOGLE_TASKS_CREDENTIALS or not SPREADSHEET_ID:
    return None
  try:
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_TASKS_CREDENTIALS),
        scopes=['https://www.googleapis.com/auth/spreadsheets'],
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID).sheet1
  except Exception:
    return None


def add_to_tasks(title, notes):
  if not GOOGLE_TASKS_CREDENTIALS:
    return
  try:
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_TASKS_CREDENTIALS),
        scopes=['https://www.googleapis.com/auth/tasks'],
    )
    build('tasks', 'v1', credentials=creds).tasks().insert(
        tasklist='@default', body={'title': title, 'notes': notes}
    ).execute()
  except Exception as e:
    print(f'❌ Błąd Tasks: {e}')


def main():
  uk_now = datetime.datetime.now(ZoneInfo('Europe/London'))
  today_str = uk_now.strftime('%Y-%m-%d')
  now_str = uk_now.strftime('%Y-%m-%d %H:%M')

  cache = {}
  if os.path.exists(CACHE_FILE):
    try:
      with open(CACHE_FILE, 'r') as f:
        cache = json.load(f)
    except Exception:
      pass

  sheet = get_google_sheet()

  # Wskaźnik referencyjny BTC do badania Siły Względnej (RS vs BTC)
  btc_perf_24h = 0.0
  try:
    btc_hist = yf.Ticker('BTC-GBP').history(period='5d', interval='1d')
    if len(btc_hist) >= 2:
      btc_perf_24h = (
          (btc_hist['Close'].iloc[-1] - btc_hist['Close'].iloc[-2])
          / btc_hist['Close'].iloc[-2]
      ) * 100
  except Exception as e:
    print(f'⚠️ Problem z pobraniem referencyjnego BTC: {e}')

  digest_lines = []

  for ticker in TICKERS:
    try:
      time.sleep(1.2)
      is_core = ticker in CORE_CRYPTO
      symbol_native = '£' if ticker.endswith('-GBP') else '$'

      crypto_icon = '🪙' if is_core else '🛰️'
      clean_name = ticker.replace('-', '/')
      ticker_link = f'[{clean_name}](https://finance.yahoo.com/quote/{ticker})'

      stock = yf.Ticker(ticker)

      # =========================================================================
      # KROK 1: ANALIZA MAKRO (1D) — TREND, ATR, BOLINGER, STREFI WSPARCIA
      # =========================================================================
      df_1d = stock.history(period='1y', interval='1d').ffill()
      clean_1d_close = df_1d['Close'].dropna()
      if len(clean_1d_close) < 50:
        continue

      try:
        price_native = float(
            stock.fast_info.get('lastPrice', clean_1d_close.iloc[-1])
        )
      except Exception:
        price_native = clean_1d_close.iloc[-1]

      price_display = f'{symbol_native}{price_native:.4f}' if price_native < 1 else f'{symbol_native}{price_native:.2f}'

      high_52w = df_1d['High'].dropna().max()
      drawdown_pct = (
          round(((price_native - high_52w) / high_52w) * 100, 1)
          if high_52w > 0
          else 0
      )

      ema200_1d = clean_1d_close.ewm(span=200, adjust=False).mean().iloc[-1]
      is_uptrend_1d = price_native > ema200_1d
      rsi_1d = calculate_rsi(clean_1d_close)

      # Zmienność i Wstęgi Bollingera (BBW)
      df_1d['BB_mid'] = clean_1d_close.rolling(window=20).mean()
      df_1d['BB_std'] = clean_1d_close.rolling(window=20).std()
      df_1d['BBW'] = (
          df_1d['BB_mid']
          + (df_1d['BB_std'] * 2)
          - (df_1d['BB_mid'] - (df_1d['BB_std'] * 2))
      ) / df_1d['BB_mid']
      bbw_avg = (
          df_1d['BBW'].dropna().rolling(window=20).mean().iloc[-1]
          if len(df_1d['BBW'].dropna()) >= 20
          else 1.0
      )
      bbw_current = (
          df_1d['BBW'].dropna().iloc[-1]
          if len(df_1d['BBW'].dropna()) > 0
          else 1.0
      )
      vol_mult = (
          (bbw_current / bbw_avg)
          if (not pd.isna(bbw_avg) and bbw_avg > 0)
          else 1.0
      )

      # ATR & Anomaly Detection
      true_range = pd.concat(
          [
              df_1d['High'] - df_1d['Low'],
              (df_1d['High'] - df_1d['Close'].shift()).abs(),
              (df_1d['Low'] - df_1d['Close'].shift()).abs(),
          ],
          axis=1,
      ).max(axis=1)
      df_1d['ATR'] = true_range.rolling(window=14).mean()
      df_1d['ATR_MA'] = df_1d['ATR'].rolling(window=20).mean()
      atr_current = df_1d['ATR'].iloc[-1]
      atr_ma_current = df_1d['ATR_MA'].iloc[-1]
      is_anomaly = (
          (atr_current > (atr_ma_current * 2.5))
          if not pd.isna(atr_ma_current) and atr_ma_current > 0
          else False
      )

      # WSPARCIE 1W (Tygodniowy Bollinger Lower)
      bb_1w_val = None
      bb_1w_msg = '📐 **Wsparcie 1W:** `Brak danych`'
      try:
        df_1w = stock.history(period='2y', interval='1wk').ffill()
        clean_1w = df_1w['Close'].dropna()
        if len(clean_1w) >= 21:
          closed_1w = clean_1w.iloc[:-1]
          bb_1w_val = (
              closed_1w.rolling(20).mean().iloc[-1]
              - (closed_1w.rolling(20).std().iloc[-1] * 2)
          )
          if not pd.isna(bb_1w_val):
            dist = (
                round(((bb_1w_val - price_native) / price_native) * 100, 1)
                if price_native > 0
                else 0
            )
            val_fmt = f'{bb_1w_val:.4f}' if bb_1w_val < 1 else f'{bb_1w_val:.2f}'
            bb_1w_msg = (
                f'📐 **Wsparcie 1W (BB):** `{symbol_native}{val_fmt}`\n📉'
                f' **Dystans do 1W:** `{dist}%`'
            )
      except:
        pass

      # WYKRYWANIE TWARDEJ STREFY WSPARCIA (Wsparcie w zasięgu <= 2.5%)
      near_supports = []
      if not pd.isna(ema200_1d) and ema200_1d > 0:
        if abs(price_native - ema200_1d) / price_native <= 0.025:
          near_supports.append('EMA 200 1D')
      if bb_1w_val is not None and not pd.isna(bb_1w_val) and bb_1w_val > 0:
        if abs(price_native - bb_1w_val) / price_native <= 0.025:
          near_supports.append('1W BB Lower')
      low_90d = df_1d['Low'].tail(90).min()
      if not pd.isna(low_90d) and low_90d > 0:
        if abs(price_native - low_90d) / price_native <= 0.02:
          near_supports.append('Dołek 3M')

      is_near_support = len(near_supports) > 0
      support_txt = ', '.join(near_supports) if is_near_support else 'Brak'

      # Siła względna względem BTC (dla Satelitów)
      is_rs_vs_btc = False
      if not is_core and len(clean_1d_close) >= 2:
        token_perf_24h = (
            (clean_1d_close.iloc[-1] - clean_1d_close.iloc[-2])
            / clean_1d_close.iloc[-2]
        ) * 100
        if token_perf_24h > (btc_perf_24h + 1.5):
          is_rs_vs_btc = True

      # =========================================================================
      # KROK 2: ANALIZA MIKRO (4H) — DYWERGENCJA 24/7, PINBAR I TRIGGER
      # =========================================================================
      df_1h = stock.history(period='1mo', interval='1h').ffill()
      rsi_4h = 50.0
      is_rsi_div = False
      is_pinbar = False

      if len(df_1h) > 50:
        # Całodobowe resamplowanie do świec 4H (Krypto działa 24/7)
        df_4h_df = (
            df_1h.resample('4h')
            .agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
            })
            .dropna()
        )

        if len(df_4h_df) >= 14:
          clean_4h = df_4h_df['Close'].dropna()
          delta_4h = clean_4h.diff()
          gain_4h = delta_4h.where(delta_4h > 0, 0)
          loss_4h = -delta_4h.where(delta_4h < 0, 0)
          avg_gain_4h = gain_4h.ewm(alpha=1 / 14, adjust=False).mean()
          avg_loss_4h = loss_4h.ewm(alpha=1 / 14, adjust=False).mean().replace(
              0, 1e-10
          )
          rs_4h = avg_gain_4h / avg_loss_4h
          df_4h_df['RSI'] = round(100 - (100 / (1 + rs_4h)), 2)

          rsi_4h = df_4h_df['RSI'].iloc[-1]
          is_rsi_div = detect_rsi_divergence(df_4h_df)

        # Weryfikacja Knotu Popytowego (Pinbar 4H)
        last_open = df_1h['Open'].iloc[-1]
        last_high = df_1h['High'].iloc[-1]
        last_low = df_1h['Low'].iloc[-1]
        candle_range = last_high - last_low
        if candle_range > 0:
          lower_wick = min(last_open, price_native) - last_low
          if (lower_wick / candle_range) >= 0.40:
            is_pinbar = True

      # =========================================================================
      # KROK 3: KWALIFIKACJA SYGNAŁU (LOGIKA MULTI-FACTOR KRYPTO)
      # =========================================================================
      if is_core:
        base_buy_thr = 36
        min_floor = 30
      else:
        base_buy_thr = 28
        min_floor = 22

      buy_thr_4h = (
          max(min_floor, base_buy_thr - 4) if vol_mult > 1.3 else base_buy_thr
      )
      sell_thr_4h = max(70, 75 if vol_mult > 1.3 else 68) if is_uptrend_1d else 50

      if is_anomaly:
        is_buy = False
        is_sell = False
      else:
        is_buy = (rsi_4h < buy_thr_4h) or (
            rsi_4h < (buy_thr_4h + 5) and is_rsi_div
        )
        is_sell = rsi_4h >= sell_thr_4h

      signal = 'NONE'
      if is_sell:
        signal = 'WYKUPIENIE'
      elif is_buy:
        if is_uptrend_1d or is_core:
          signal = 'WYPRZEDANIE_ZIELONE'
        else:
          signal = 'WYPRZEDANIE_ZOLTE'

      # Formatowanie tagów statusu
      status_tag = []
      if is_rsi_div:
        status_tag.append('🎯 DYWERGENCJA')
      if is_near_support:
        status_tag.append('🛡️ WSPARCIE')
      if is_pinbar:
        status_tag.append('🕯️ PINBAR')
      if is_rs_vs_btc:
        status_tag.append('🚀 RS vs BTC')
      tag_str = f" [{', '.join(status_tag)}]" if status_tag else ''

      status_txt = {
          'NONE': '⚪ Neutralny',
          'WYPRZEDANIE_ZIELONE': f'🟢 MEGA OKAZJA{tag_str}',
          'WYPRZEDANIE_ZOLTE': f'🟡 Dołek 4H{tag_str}',
          'WYKUPIENIE': '🟠 Take Profit / 🔴 Ewakuacja',
      }.get(signal, '⚪ Neutralny')

      trend_txt = '↗️' if is_uptrend_1d else '↘️'
      digest_lines.append(
          f'{crypto_icon} {ticker_link} — {price_display}\n  └ RSI 4h:'
          f' {rsi_4h} | Trend: {trend_txt} | Stan: {status_txt}\n'
      )

      # =========================================================================
      # KROK 4: WYSYŁANIE ALERTÓW (NATYCHMIAST 24/7)
      # =========================================================================
      last_sig = cache.get(ticker, {}).get('signal', 'NONE')
      last_date = cache.get(ticker, {}).get('date', '')

      if signal != 'NONE' and (last_date != today_str or last_sig != signal):
        ranga_label = 'Core 🏛️' if is_core else 'Satelita 🛰️'
        confirmations = []
        if is_rsi_div:
          confirmations.append('🎯 **Bycza Dywergencja RSI (4H)**')
        if is_near_support:
          confirmations.append(f'🛡️ **Strefa Wsparcia:** {support_txt}')
        if is_pinbar:
          confirmations.append('🕯️ **Knot Popytowy (Pinbar 4H)**')
        if is_rs_vs_btc:
          confirmations.append('🚀 **Siła Względna:** Wyprzedza BTC')
        conf_msg = '\n' + '\n'.join(confirmations) if confirmations else ''

        if signal == 'WYKUPIENIE':
          if is_uptrend_1d:
            title = f'TAKE PROFIT: {clean_name} (RSI {rsi_4h})'
            body = (
                f'🟠 **REALIZACJA ZYSKU (KRYPTO)!**\n\n{crypto_icon}'
                f' **Aktywo:** {ticker_link}\n💰 **Cena:**'
                f' {price_display}\n📉 **Od szczytu (52W):**'
                f' {drawdown_pct}%\n💪 **Ranga:** {ranga_label}\n📊 **RSI'
                f' 4H:** {rsi_4h} (Próg: {sell_thr_4h})\n📊 **RSI 1d:**'
                f' {rsi_1d}'
            )
          else:
            title = f'🔴 PILNA OPERACJA: SPRZEDAJ {clean_name}!'
            body = (
                f'🔴 **EWAKUACJA! TREND SPADKOWY! NATYCHMIAST SPRZEDAJ'
                f' {clean_name}!**\n🔴 **WYMAGANA PILNA OPERACJA NA RYNKU'
                f' KRYPTO!**\n\n🔴 **AKTYWO:** {ticker_link}\n🔴 **CENA:**'
                f' {price_display}\n🔴 **RSI 4H:** {rsi_4h} (ODBICIE OD'
                f' OPORU)'
            )
        elif signal == 'WYPRZEDANIE_ZIELONE':
          title = f'MEGA OKAZJA KRYPTO: KUP {clean_name}'
          body = (
              f'🟢 **MEGA OKAZJA / HOSSA (DOŁEK KRYPTO)**\n\n{crypto_icon}'
              f' **Aktywo:** {ticker_link}\n💰 **Cena:** {price_display}\n📉'
              f' **Od szczytu (52W):** {drawdown_pct}%\n💪 **Ranga:**'
              f' {ranga_label}\n📊 **RSI 4h:** {rsi_4h} (Próg:'
              f' {buy_thr_4h})\n📊 **RSI 1d:** {rsi_1d}\n{bb_1w_msg}{conf_msg}'
          )
        else:
          title = f'SWING KRYPTO: OBSERWUJ {clean_name}'
          body = (
              f'🟡 **OKAZJA SWING / {ranga_label.upper()} (DOŁEK 4H)**\n\n'
              f'{crypto_icon} **Aktywo:** {ticker_link}\n💰 **Cena:**'
              f' {price_display}\n📉 **Od szczytu (52W):**'
              f' {drawdown_pct}%\n💪 **Ranga:** {ranga_label}\n📊 **RSI'
              f' 4h:** {rsi_4h} (Próg: {buy_thr_4h})\n📊 **RSI 1d:**'
              f' {rsi_1d}\n{bb_1w_msg}{conf_msg}'
          )

        send_telegram(body)
        add_to_tasks(title, body.replace('**', '').replace('`', ''))

        if sheet:
          try:
            sheet.append_row(
                [now_str, ticker, price_display, rsi_4h, rsi_1d, signal]
            )
          except:
            pass

        cache[ticker] = {'signal': signal, 'date': today_str}

    except Exception as e:
      err_msg = f'⚠️ **Błąd skanera (Krypto) dla {ticker}:** `{str(e)}`'
      print(err_msg)
      send_telegram(err_msg)

  if uk_now.hour >= 21 and cache.get('DIGEST_DATE') != today_str:
    if digest_lines:
      digest_msg = (
          '📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n'
          + ''.join(digest_lines)
      )
      send_telegram(digest_msg)
      add_to_tasks(
          f'Podsumowanie Krypto ({today_str})',
          digest_msg.replace('**', '').replace('`', ''),
      )
      cache['DIGEST_DATE'] = today_str

  with open(CACHE_FILE, 'w') as f:
    json.dump(cache, f)


if __name__ == '__main__':
  try:
    main()
  except Exception as e:
    send_telegram(f'🚨 **KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`')
    raise e
