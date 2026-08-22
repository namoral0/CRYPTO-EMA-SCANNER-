import os
import ccxt
import pandas as pd
import requests
import json
import time
from datetime import datetime, timezone

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

# Zmienne środowiskowe z GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GOOGLE_TASKS_CREDENTIALS = os.getenv("GOOGLE_TASKS_CREDENTIALS")

SYMBOLS = [
    "XRP/GBP", "BTC/GBP", "ETH/GBP", "LINK/GBP", "SOL/GBP", 
    "ONDO/USD", "SUI/GBP", "AAVE/GBP", "AVAX/USD", "NEAR/USD",
    "TAO/USD", "RENDER/USD"
]
CACHE_FILE = "cache.json"

def send_telegram_alert(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Brak TELEGRAM_TOKEN lub TELEGRAM_CHAT_ID w środowisku!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    print(f"Telegram status: {response.status_code}")

def add_to_tasks(title, notes):
    if not HAS_GOOGLE or not GOOGLE_TASKS_CREDENTIALS: 
        print("⚠️ Pomijam Google Tasks (brak bibliotek lub credentials).")
        return
    try:
        creds_dict = json.loads(GOOGLE_TASKS_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/tasks"])
        service = build('tasks', 'v1', credentials=creds)
        service.tasks().insert(tasklist='@default', body={'title': title, 'notes': notes}).execute()
        print("✅ Pomyślnie dodano do Google Tasks")
    except Exception as e:
        print(f"❌ Błąd Tasks: {e}")

def main():
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
    
    usd_gbp_rate = 0.78
    try:
        ticker_fx = exchange.fetch_ticker("GBP/USD")
        if ticker_fx and ticker_fx.get('last'):
            usd_gbp_rate = 1.0 / ticker_fx['last']
    except Exception:
        pass

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest_lines = []

    # Słownik buforujący dane BTC, aby nie pytać giełdy wielokrotnie o to samo
    btc_data_cache = {}

    for symbol in SYMBOLS:
        try:
            time.sleep(1.5) # Bezpieczny odstęp czasowy chroniący przed rate limitami

            quote_currency = symbol.split('/')[1]
            btc_symbol = f"BTC/{quote_currency}"

            # Pobieranie danych BTC jednorazowo dla danej waluty bazowej
            if btc_symbol not in btc_data_cache:
                time.sleep(1)
                df_1d_btc = pd.DataFrame(exchange.fetch_ohlcv(btc_symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
                time.sleep(1)
                df_4h_btc = pd.DataFrame(exchange.fetch_ohlcv(btc_symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
                btc_data_cache[btc_symbol] = {
                    "1d": df_1d_btc,
                    "4h": df_4h_btc
                }

            df_1d_btc = btc_data_cache[btc_symbol]["1d"]
            df_4h_btc = btc_data_cache[btc_symbol]["4h"]

            # 1. Pobieranie danych 1D dla monety
            df_1d = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="1d", limit=250), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            ema_200_1d = df_1d['close'].ewm(span=200, adjust=False).mean().iloc[-1]
            is_uptrend_1d = df_1d['close'].iloc[-1] > ema_200_1d

            rs_1d = df_1d['close'] / df_1d_btc['close']
            rs_1d_ema = rs_1d.ewm(span=20, adjust=False).mean()
            is_strong_vs_btc_1d = (rs_1d.iloc[-2] >= rs_1d_ema.iloc[-2]) if symbol != btc_symbol else True
            
            # 2. Pobieranie danych 4H dla monety
            df_4h = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="4h", limit=300), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            df_4h['EMA_20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
            df_4h['EMA_50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
            
            delta_4h = df_4h['close'].diff()
            gain_4h = delta_4h.where(delta_4h > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_4h = (-delta_4h.where(delta_4h < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_4h = gain_4h / loss_4h
            df_4h['RSI'] = 100 - (100 / (1 + rs_4h))
            
            df_4h['BB_mid'] = df_4h['close'].rolling(window=20).mean()
            df_4h['BB_std'] = df_4h['close'].rolling(window=20).std()
            df_4h['BB_upper'] = df_4h['BB_mid'] + (df_4h['BB_std'] * 2)
            df_4h['BB_lower'] = df_4h['BB_mid'] - (df_4h['BB_std'] * 2)
            df_4h['BBW'] = (df_4h['BB_upper'] - df_4h['BB_lower']) / df_4h['BB_mid']
            df_4h['BBW_MA'] = df_4h['BBW'].rolling(window=20).mean()

            high_low = df_4h['h'] - df_4h['l']
            high_close = (df_4h['h'] - df_4h['close'].shift()).abs()
            low_close = (df_4h['l'] - df_4h['close'].shift()).abs()
            true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
            df_4h['ATR'] = true_range.rolling(window=14).mean()
            df_4h['ATR_MA'] = df_4h['ATR'].rolling(window=20).mean()

            df_4h['Vol_MA'] = df_4h['v'].rolling(window=20).mean()

            df_4h['RS'] = df_4h['close'] / df_4h_btc['close']
            df_4h['RS_EMA'] = df_4h['RS'].ewm(span=20, adjust=False).mean()
            is_strong_vs_btc_4h = (df_4h['RS'].iloc[-2] >= df_4h['RS_EMA'].iloc[-2]) if symbol != btc_symbol else True

            # 3. Pobieranie danych 15m dla monety
            df_15m = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe="15m", limit=100), columns=['ts', 'o', 'h', 'l', 'close', 'v'])
            delta_15m = df_15m['close'].diff()
            gain_15m = delta_15m.where(delta_15m > 0, 0).ewm(alpha=1/14, adjust=False).mean()
            loss_15m = (-delta_15m.where(delta_15m < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
            rs_15m = gain_15m / loss_15m
            df_15m['RSI'] = 100 - (100 / (1 + rs_15m))

            ts_closed = int(df_4h['ts'].iloc[-2])
            rsi_4h_closed = round(df_4h['RSI'].iloc[-2], 1)
            rsi_15m_live = round(df_15m['RSI'].iloc[-1], 1)
            
            close_closed = df_4h['close'].iloc[-2]
            bb_upper_closed = df_4h['BB_upper'].iloc[-2]
            bb_lower_closed = df_4h['BB_lower'].iloc[-2]
            current_atr = df_4h['ATR'].iloc[-2]
            avg_atr = df_4h['ATR_MA'].iloc[-2]
            bbw_closed = df_4h['BBW'].iloc[-2]
            bbw_avg = df_4h['BBW_MA'].iloc[-2]
            current_vol = df_4h['v'].iloc[-2]
            avg_vol = df_4h['Vol_MA'].iloc[-2]

            is_anomaly_candle = current_atr > (avg_atr * 2.5) if not pd.isna(avg_atr) else False
            is_volume_spike = current_vol > (avg_vol * 2.0) if not pd.isna(avg_vol) else False
            
            if symbol.endswith("/USD"):
                price_gbp = close_closed * usd_gbp_rate
                display_price = f"£{price_gbp:.4f} (${close_closed:.4f})"
                display_symbol = symbol.replace("/USD", "/GBP")
            else:
                display_price = f"£{close_closed:.4f}"
                display_symbol = symbol
            
            if "BTC" in symbol or "ETH" in symbol:
                base_buy, base_sell = 30, 70
                confirm_15m_buy, confirm_15m_sell = 40, 60
                profile_type = "Blue-chip"
            else:
                base_buy, base_sell = 25, 75
                confirm_15m_buy, confirm_15m_sell = 35, 65
                profile_type = "Altcoin"

            volatility_multiplier = (bbw_closed / bbw_avg) if (not pd.isna(bbw_avg) and bbw_avg > 0) else 1.0
            if volatility_multiplier > 1.3:
                buy_rsi_threshold = max(20, base_buy - 3)
                sell_rsi_threshold = min(80, base_sell + 3)
            elif volatility_multiplier < 0.7:
                buy_rsi_threshold = min(35, base_buy + 3)
                sell_rsi_threshold = max(65, base_sell - 3)
            else:
                buy_rsi_threshold = base_buy
                sell_rsi_threshold = base_sell

            crossover_up = (df_4h['EMA_20'].iloc[-3] <= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] > df_4h['EMA_50'].iloc[-2])
            crossover_down = (df_4h['EMA_20'].iloc[-3] >= df_4h['EMA_50'].iloc[-3] and df_4h['EMA_20'].iloc[-2] < df_4h['EMA_50'].iloc[-2])
            
            is_oversold_4h = rsi_4h_closed <= buy_rsi_threshold
            is_overbought_4h = rsi_4h_closed >= sell_rsi_threshold
            
            price_above_upper_bb = close_closed >= bb_upper_closed
            price_below_lower_bb = close_closed <= bb_lower_closed
            
            is_buy = (
                ((is_oversold_4h and rsi_15m_live <= confirm_15m_buy) or (crossover_up and is_uptrend_1d) or (is_oversold_4h and price_below_lower_bb)) 
                and not is_anomaly_candle 
                and is_volume_spike 
                and is_strong_vs_btc_4h 
                and is_strong_vs_btc_1d
            )
            
            is_sell = (
                ((is_overbought_4h and rsi_15m_live >= confirm_15m_sell) or crossover_down or (is_overbought_4h and price_above_upper_bb)) 
                and not is_anomaly_candle 
                and is_volume_spike
            )

            current_signal_type = "NONE"
            if is_buy:
                current_signal_type = "BUY"
            elif is_sell:
                current_signal_type = "SELL"

            digest_lines.append(f"• `{display_symbol}`: RSI 4H `{rsi_4h_closed}` | RS vs BTC: `{'Silny 🟢' if is_strong_vs_btc_4h else 'Słaby 🔴'}` | Stan: `{current_signal_type}`")

            cached_data = cache.get(symbol, {})
            cached_ts = cached_data.get("ts")
            cached_signal = cached_data.get("signal")
            last_digest_date = cached_data.get("last_digest_date", "")

            should_alert = current_signal_type != "NONE" and (cached_ts != ts_closed or cached_signal != current_signal_type)

            print(f"[{display_symbol}] Cena: {display_price} | RSI 4H (Zamknięta): {rsi_4h_closed} | RS vs BTC: {is_strong_vs_btc_4h} | Stan: {current_signal_type}")
            
            if should_alert:
                if current_signal_type == "SELL":
                    rodzaj = "**🔴 KRYTYCZNE ZAGROŻENIE: ROZWAŻ SPRZEDAŻ! 🔴**"
                    task_title = f"PILNE: SPRZEDAJ {display_symbol} (Zagrożenie/RSI {sell_rsi_threshold})"
                else:
                    rodzaj = "**🟢 OKAZJA ZAKUPOWA (Lider RS / Wolumen)**"
                    task_title = f"KUP {display_symbol} (Sygnał wejścia RSI {buy_rsi_threshold})"
                    
                msg = (
                    f"{rodzaj}\n\n"
                    f"🪙 **Moneta:** `{display_symbol}` ({profile_type})\n"
                    f"💰 **Cena:** `{display_price}`\n"
                    f"📊 **RSI 4H (Zamknięta Świeca):** `{rsi_4h_closed} / Próg: {sell_rsi_threshold if current_signal_type=='SELL' else buy_rsi_threshold}`\n"
                    f"💪 **Siła Wzgl. (RS vs BTC):** `Potwierdzona (Outperformer)`\n"
                    f"📊 **Skok wolumenu:** `TAK (Volume Spike)`"
                )
                
                send_telegram_alert(msg)
                add_to_tasks(task_title, msg)

            cache[symbol] = {
                "ts": ts_closed,
                "signal": current_signal_type,
                "last_digest_date": today_str if last_digest_date != today_str else last_digest_date
            }
                
        except Exception as e:
            print(f"❌ Błąd dla {symbol}: {e}")
            continue

    any_symbol_cache = list(cache.values())[0] if cache else {}
    if any_symbol_cache.get("last_digest_date") != today_str:
        digest_msg = "📋 **CODZIENNE PODSUMOWANIE RYNKU (DIGEST + RS)** 📋\n\n" + "\n".join(digest_lines)
        send_telegram_alert(digest_msg)
        add_to_tasks(f"Codzienny Digest Krypto ({today_str})", digest_msg)
        
        for sym in cache:
            cache[sym]["last_digest_date"] = today_str

    with open(CACHE_FILE, "w") as f: 
        json.dump(cache, f)

if __name__ == "__main__":
    main()
                
