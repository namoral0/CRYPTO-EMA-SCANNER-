import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd
import requests

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

CORE_CRYPTO = ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
SYMBOLS = [
    "TAO/GBP", "BTC/GBP", "ETH/GBP", "ONDO/GBP", "SOL/GBP", 
    "XRP/GBP", "RENDER/GBP", "SUI/GBP", "LINK/GBP", "AAVE/GBP"
]
CACHE_FILE = "cache_krypto.json"

def send_telegram_alert(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"❌ Nie udało się wysłać wiadomości na Telegram: {e}")

def add_to_tasks(title, notes):
    if not HAS_GOOGLE or not GOOGLE_TASKS_CREDENTIALS:
        return
    try:
        creds_dict = json.loads(GOOGLE_TASKS_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/tasks"])
        service = build('tasks', 'v1', credentials=creds)
        service.tasks().insert(tasklist='@default', body={'title': title, 'notes': notes}).execute()
    except Exception as e:
        print(f"❌ Błąd Tasks: {e}")

def main():
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    today_str = uk_now.strftime("%Y-%m-%d")
    
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f: 
                loaded_cache = json.load(f)
                for k, v in loaded_cache.items():
                    if isinstance(v, int):
                        cache[k] = {"ts": v, "signal": "NONE", "last_digest_date": ""}
                    elif isinstance(v, dict):
                        cache[k] = v
        except Exception:
            pass
    
    exchange = ccxt.kraken({'enableRateLimit': True})
    
    digest_lines = []
    btc_data_cache = {}
    pending_alerts = []

    for symbol in SYMBOLS:
        try:
            time.sleep(1.2)
            is_core = symbol in CORE_CRYPTO
            quote_currency = symbol.split('/')[1]
            btc_symbol = f"BTC/{quote_currency}"

            if btc_symbol not in btc_data_cache:
                time.sleep(1)
                df_1d_btc = pd.DataFrame(exchange.fetch_ohlcv(btc_symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
                time.sleep(1)
                df_4h_btc = pd.DataFrame(exchange.fetch_ohlcv(btc_symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
                btc_data_cache[btc_symbol] = {"1d": df_1d_btc, "4h": df_4h_btc}

            df_4h_btc = btc_data_cache[btc_symbol]["4h"]

            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            if len(df_1d) < 30:
                continue  
            
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d

            df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            if len(df_4h) < 100:
                continue
            
            ts_closed = int(df_4h['ts'].iloc[-2])
            
            df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            
            delta_4h = df_4h['close'].diff()
            gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
            df_4h['RSI'] = 100 - (100 / (1 + (gain_4h / loss_4h)))
            
            df_4h['BB_mid'] = df_4h['close'].rolling(window=20).mean()
            df_4h['BB_std'] = df_4h['close'].rolling(window=20).std()
            df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
            df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)
            df_4h['BBW'] = (df_4h['BB_upper'] - df_4h['BB_lower']) / df_4h['BB_mid']
            df_4h['BBW_MA'] = df_4h['BBW'].rolling(window=20).mean()

            true_range = pd.concat([df_4h['h'] - df_4h['l'], (df_4h['h'] - df_4h['close'].shift()).abs(), (df_4h['l'] - df_4h['close'].shift()).abs()], axis=1).max(axis=1)
            df_4h['ATR'] = true_range.rolling(window=14).mean()
            df_4h['ATR_MA'] = df_4h['ATR'].rolling(window=20).mean()
            df_4h['Vol_MA'] = df_4h['v'].rolling(window=20).mean()

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

            df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="15m", limit=100), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            if len(df_15m) < 20:
                continue
            delta_15m = df_15m['close'].diff()
            gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean().replace(0, 1e-10)
            df_15m['RSI'] = 100 - (100 / (1 + (gain_15m / loss_15m)))

            rsi_4h_closed = round(df_4h['RSI'].iloc[-2], 1)
            rsi_15m_live = round(df_15m['RSI'].iloc[-1], 1)
            
            close_closed = df_4h['close'].iloc[-2]
            bb_upper_closed = df_4h['BB_upper'].iloc[-2]
            bb_lower_closed = df_4h['BB_lower'].iloc[-2]
            avg_atr = df_4h['ATR_MA'].iloc[-2]
            bbw_closed = df_4h['BBW'].iloc[-2]
            bbw_avg = df_4h['BBW_MA'].iloc[-2]
            
            current_vol = df_4h['v'].iloc[-2]
            avg_vol = df_4h['Vol_MA'].iloc[-2]
            vol_multiplier = current_vol / avg_vol if not pd.isna(avg_vol) and avg_vol > 0 else 0
            is_volume_spike = vol_multiplier > 1.4
            is_anomaly_candle = df_4h['ATR'].iloc[-2] > (avg_atr * 2.5) if not pd.isna(avg_atr) and avg_atr > 0 else False
            
            display_price = f"£{close_closed:.4f}"
            display_symbol = symbol
            
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
                is_base_buy = ((is_oversold_4h and rsi_15m_live <= confirm_15m_buy) or (crossover_up and is_uptrend_1d) or (is_oversold_4h and close_closed <= bb_lower_closed)) and not is_anomaly_candle
            else:
                is_base_buy = (is_oversold_4h and (rsi_15m_live <= confirm_15m_buy or close_closed <= bb_lower_closed)) and not is_anomaly_candle

            is_base_sell = ((is_overbought_4h and rsi_15m_live >= confirm_15m_sell) or crossover_down or (is_overbought_4h and close_closed >= bb_upper_closed)) and not is_anomaly_candle

            current_signal_type = "NONE"
            if is_base_buy:
                current_signal_type = "BUY_MEGA" if (is_volume_spike or is_core) else "BUY_SWING"
            elif is_base_sell:
                if is_core:
                    current_signal_type = "SELL_TAKE_PROFIT"
                else:
                    current_signal_type = "SELL_TAKE_PROFIT" if is_uptrend_1d else "SELL_EVACUATION"

            status_map = {
                "NONE": "⚪️ Neutralny",
                "BUY_MEGA": "🟢 MEGA OKAZJA",
                "BUY_SWING": "🟡 Dołek 4H",
                "SELL_TAKE_PROFIT": "🟠 Take Profit",
                "SELL_EVACUATION": "🔴 Ewakuacja"
            }
            status_txt = status_map.get(current_signal_type, "Neutralny")
            trend_txt = "↗️" if is_uptrend_1d else "↘️"
            
            digest_lines.append(f"🪙 **{display_symbol}** — {display_price}\n  └ RSI: {rsi_4h_closed} | Trend: {trend_txt} | Stan: {status_txt}\n")

            cached_data = cache.get(symbol, {})
            last_ts = cached_data.get("ts", 0)
            last_signal = cached_data.get("signal", "NONE")

            should_alert = False
            if current_signal_type != "NONE":
                if last_ts != ts_closed:
                    should_alert = True
                elif last_signal != current_signal_type:
                    should_alert = True
            
            signal_to_save = current_signal_type if current_signal_type != "NONE" else (last_signal if last_ts == ts_closed else "NONE")

            if should_alert:
                if current_signal_type == "BUY_MEGA":
                    rodzaj = "🟢 **MEGA OKAZJA CORE / HOSSA (KUPNO DOŁKA)**"
                    task_title = f"MEGA OKAZJA: KUP {display_symbol}"
                    msg = (
                        f"{rodzaj}\n\n"
                        f"🪙 **Moneta:** `{display_symbol}`\n"
                        f"💰 **Cena:** `{display_price}`\n"
                        f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {buy_rsi_threshold}`\n"
                        f"💪 **Typ monety:** {'FILAR CORE 🚀' if is_core else 'Satelita 🛰'}\n"
                        f"📈 **Trend 1D:** {'Wzrostowy 🟢' if is_uptrend_1d else 'Korekta w Bessie (Okazja Core) 🟡'}"
                    )
                elif current_signal_type == "BUY_SWING":
                    rodzaj = "🟡 **OKAZJA SWING / SATELITA (LOKALNY DOŁEK)**"
                    task_title = f"SWING: SPRAWDŹ {display_symbol}"
                    msg = (
                        f"{rodzaj}\n\n"
                        f"🪙 **Moneta:** `{display_symbol}`\n"
                        f"💰 **Cena:** `{display_price}`\n"
                        f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {buy_rsi_threshold}`\n"
                        f"💪 **Siła Wzgl. (BTC):** {'Outperformer 🟢' if is_strong_vs_btc_4h else 'Neutralna/Słabsza 🟡'}\n"
                        f"📈 **Trend 1D:** {'Wzrostowy 🟢' if is_uptrend_1d else 'Spadkowy 🔴'}"
                    )
                elif current_signal_type == "SELL_TAKE_PROFIT":
                    rodzaj = "🟠 **LOKALNE WYKUPIENIE: REALIZUJ ZYSKI (TAKE PROFIT)**"
                    task_title = f"TAKE PROFIT: {display_symbol} (RSI {rsi_4h_closed})"
                    msg = (
                        f"{rodzaj}\n\n"
                        f"🪙 **Moneta:** `{display_symbol}`\n"
                        f"💰 **Cena:** `{display_price}`\n"
                        f"📊 **RSI 4H:** `{rsi_4h_closed} / Próg: {sell_rsi_threshold}`\n"
                        f"💪 **Typ monety:** {'FILAR CORE 🚀' if is_core else 'Satelita 🛰'}"
                    )
                else: 
                    rodzaj = "🔴 KRYTYCZNA EWAKUACJA: SPRZEDAŻ SATELITY W TRENDZIE SPADKOWYM!"
                    task_title = f"🔴 PILNE: SPRZEDAJ {display_symbol.upper()}"
                    msg = (
                        f"🚨 **{rodzaj}**\n\n"
                        f"🔴 **MONETA: {display_symbol.upper()}**\n"
                        f"🔴 **CENA: {display_price.upper()}**\n"
                        f"🔴 **RSI 4H: {rsi_4h_closed} (PRÓG: {sell_rsi_threshold})**\n"
                        f"🔴 **TREND 1D: SPADKOWY**"
                    )

                pending_alerts.append((task_title, msg, display_symbol, status_txt))

            cache[symbol] = {"ts": ts_closed, "signal": signal_to_save, "last_digest_date": cached_data.get("last_digest_date", "")}
        
        except Exception as e:
            err_msg = f"⚠️ **Błąd skanera dla {symbol}:** `{str(e)}`"
            print(err_msg)
            send_telegram_alert(err_msg)

    if pending_alerts:
        if len(pending_alerts) <= 3:
            for task_title, msg, _, _ in pending_alerts:
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg.replace('**', ''))
        else:
            lawina_title = f"🚨 LAWINOWA ZMIANA NA RYNKU: {len(pending_alerts)} ALERTÓW!"
            lawina_msg = f"⚠️ **ZMASOWANA ZMIANA STANU RYNKU ({len(pending_alerts)} MONET)**\n\nWygaszenie pojedynczych alertów – rynek reaguje grupowo:\n\n"
            for _, _, sym, stat in pending_alerts:
                lawina_msg += f"• **{sym}**: {stat}\n"
            lawina_msg += "\n*Sprawdź szczegóły w aplikacji lub poczekaj na digest.*"
            
            send_telegram_alert(lawina_msg)
            add_to_tasks(lawina_title, lawina_msg.replace('**', ''))

    any_cache_date = ""
    for v in cache.values():
        if isinstance(v, dict) and "last_digest_date" in v:
            any_cache_date = v["last_digest_date"]
            break
            
    if uk_now.hour >= 21 and any_cache_date != today_str and digest_lines:
        digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (KRYPTO)**\n\n" + "".join(digest_lines)
        send_telegram_alert(digest_msg)
        add_to_tasks(f"Codzienny Digest ({today_str})", digest_msg.replace('**', ''))
        
        for sym in cache: 
            cache[sym]["last_digest_date"] = today_str

    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_telegram_alert(f"🚨 **🔴 KRYTYCZNY BŁĄD SKANERA KRYPTO:**\n`{str(e)}`")
        add_to_tasks('KRYTYCZNY BŁĄD SKANERA KRYPTO', str(e))
        raise e
