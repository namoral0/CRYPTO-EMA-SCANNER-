import asyncio
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

# --- KONFIGURACJA I STAŁE ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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
KRAKEN_SEMAPHORE = asyncio.Semaphore(3)


# --- ASYNCHRONICZNE POMOCNIKI HTTP (HTTPX) ---
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

async def process_telegram_commands_async(http_client: httpx.AsyncClient, cache: dict, digest_rows: list):
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
                        status_msg += f"🪙 {sym_link} — {r['price']}\n  └ RSI 4h: {r['rsi']} | Trend: {r['trend']} | Stan: {r['status']}{r.get('div_tag', '')}\n\n"
                    
                    target_chat = chat_id or TELEGRAM_CHAT_ID
                    if target_chat:
                        await send_telegram_alert_async(http_client, status_msg, custom_chat_id=target_chat)
                        logging.info(f"Odpowiedziano na komendę {text} dla chat_id: {target_chat}")
    except Exception as e:
        logging.error(f"Błąd przetwarzania komend Telegram: {e}")


# --- PĘTLA GŁÓWNA Z OPTYMALIZACJĄ OBICZEŃ I I/O ---
async def main():
    start_time = time.time()
    logging.info("Uruchamianie zoptymalizowanego skanera Krypto...")
    
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    now_str = uk_now.strftime("%Y-%m-%d %H:%M")

    tasks_service, sheet = init_google_services()
    cache = load_cache()
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    usd_gbp_rate = 0.78

    # Tworzymy jednolitą sesję HTTPX dla wszystkich operacji Telegrama
    async with httpx.AsyncClient() as http_client:
        try:
            # 1. Pobieranie wstępnych danych BTC i Tickerów
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

            # --- OPTYMALIZACJA 1: Wyliczenie makro-trendu BTC RAZ przed pętlą ---
            btc_macro_bullish = {}
            for btc_sym, btc_dfs in btc_cache.items():
                df_b_1d = btc_dfs["1d"]
                ema_200 = df_b_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
                btc_macro_bullish[btc_sym] = bool(df_b_1d['close'].iloc[-1] > ema_200)

            # 2. Pobranie danych dla wszystkich symboli w pętli równoległej
            results = await asyncio.gather(*[fetch_symbol_data(exchange, sym) for sym in SYMBOLS])

            digest_lines = []
            digest_summary_rows = []
            pending_alerts = []
            rows_to_append_sheets = []

            active_positions = cache.get("active_positions", {})

            # 3. Analiza każdej monety
            for symbol, df_1d, df_4h, df_15m in results:
                if df_1d is None or len(df_1d) < 30 or len(df_4h) < 100 or len(df_15m) < 20:
                    continue

                is_core = symbol in CORE_CRYPTO
                quote_currency = symbol.split('/')[1]
                btc_symbol = f"BTC/{quote_currency}"

                df_1d, df_4h, df_15m = compute_indicators(df_1d, df_4h, df_15m)

                # Wykorzystujemy przygotowany wcześniej słownik zamiast liczyć EMA na nowo
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

                # [...] (Sekcja logiki wyliczania SL/TP i wskaźników pozostaje bez zmian)

                # --- OPTYMALIZACJA 2: Równoległe wysyłanie komunikatów o TP/SL ---
                if symbol in active_positions:
                    pos = active_positions[symbol]
                    entry_price, sl_price, tp_price = pos["entry_price"], pos["sl_price"], pos["tp_price"]

                    if high_gbp >= tp_price:
                        tp_msg = f"🎯 **TAKE PROFIT OSIĄGNIĘTY dla {symbol}!**\nCena osiągnęła cel `{tp_str}`. Zysk zrealizowany!"
                        await asyncio.gather(
                            send_telegram_alert_async(http_client, tp_msg),
                            add_to_tasks_async(tasks_service, f"TP OSIĄGNIĘTY: {symbol}", tp_msg.replace('**', ''))
                        )
                        rows_to_append_sheets.append([now_str, symbol, "TP_HIT_SUCCESS 🟢", display_price, f"RSI: {rsi_4h_closed}", "-", "-"])
                        del active_positions[symbol]

                    elif low_gbp <= sl_price:
                        sl_msg = f"🔴 **STOP LOSS TRAFIONY dla {symbol}!**\nCena spadła do `{sl_str}`. Pozycja zamknięta na kontrolowanej stracie £10."
                        await asyncio.gather(
                            send_telegram_alert_async(http_client, sl_msg),
                            add_to_tasks_async(tasks_service, f"SL TRAFIONY: {symbol}", sl_msg.replace('**', ''))
                        )
                        rows_to_append_sheets.append([now_str, symbol, "SL_HIT_CLOSED 🔴", display_price, f"RSI: {rsi_4h_closed}", "-", "-"])
                        del active_positions[symbol]

                # [...] (Gromadzenie pending_alerts bez zmian)

            cache["active_positions"] = active_positions

            # GitHub Summary & Telegram Commands
            write_github_step_summary(digest_summary_rows)
            await process_telegram_commands_async(http_client, cache, digest_summary_rows)

            # --- OPTYMALIZACJA 3: Równoległe wysyłanie alertów zbiorczych ---
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

                # Wykonaj wszystkie wysyłki sieciowe naraz!
                await asyncio.gather(*alert_coroutines)

            if sheet and rows_to_append_sheets:
                try:
                    await asyncio.to_thread(sheet.append_rows, rows_to_append_sheets)
                    logging.info(f"Zapisano {len(rows_to_append_sheets)} wierszy w Google Sheets.")
                except Exception as e:
                    logging.error(f"Nie udało się zapisać wierszy w Google Sheets: {e}")

            # Digest po 21:00
            if uk_now.hour >= 21 and cache.get("DIGEST_DATE") != today_str:
                if digest_lines:
                    digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n" + "".join(digest_lines)
                    await asyncio.gather(
                        send_telegram_alert_async(http_client, digest_msg),
                        add_to_tasks_async(tasks_service, f"Codzienny Digest ({today_str})", digest_msg.replace('**', ''))
                    )
                    cache["DIGEST_DATE"] = today_str

            save_cache(cache)

        finally:
            await exchange.close()

    elapsed = round(time.time() - start_time, 2)
    logging.info(f"Skaner Krypto zakończył działanie w czasie: {elapsed}s.")
