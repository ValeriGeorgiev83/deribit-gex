import os
import math
import json
import requests
import pandas as pd
import numpy as np
import flet as ft
import threading
import time
from datetime import datetime, timezone, timedelta 

# Initialize Upstash Redis with your EXACT verified connection parameters
from upstash_redis import Redis
redis = Redis(
    url="https://large-ghost-131173.upstash.io",
    token="gQAAAAAAAgBlAAIgcDE2NmI0NGZkNDFiYTk0NzlhOWJmZGM1MTg5OWViZDIxMw"
)
REDIS_FLOW_KEY = "deribit_flow_24h_history"
REDIS_OI_MIGRATION_KEY = "deribit_oi_hourly_history"
MAX_HISTORY_POINTS = 3500

# Keep track of the previous ATM IV to find live volatility direction velocity
last_known_atm_iv = [50.0] 

def native_norm_pdf(x):
    """Pure mathematical replacement for scipy.stats.norm.pdf to prevent deployment crashes."""
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * (x ** 2)) 

def native_norm_cdf(x):
    """Pure mathematical approximation for standard normal cumulative distribution function (CDF)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))) 

def calculate_speed_for_option(spot, strike, iv, t_days, oi, option_type):
    """Calculates Option Speed (dGamma/dSpot) contract footprint mathematically scaled for BTC."""
    if t_days <= 0 or iv <= 0 or oi <= 0:
        return 0.0
    try:
        t = t_days / 365.0
        d1 = (math.log(spot / strike) + (0.5 * (iv ** 2)) * t) / (iv * math.sqrt(t))
        pdf = native_norm_pdf(d1) 

        gamma = pdf / (spot * iv * math.sqrt(t))
        speed_per_contract = (-gamma / spot) * (1.0 + (d1 / (iv * math.sqrt(t)))) 

        footprint = oi * speed_per_contract * 0.01 * 1000000.0
        return -footprint if option_type == 'P' else footprint
    except Exception:
        return 0.0 

def calculate_realized_vol_10d(currency="BTC"):
    """Fetches the last 10 daily close prices to calculate annualized close-to-close realized volatility."""
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        start_ts = now_ts - (12 * 86400) 

        url = f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name={currency.upper()}-USD&resolution=1D&start_timestamp={start_ts * 1000}&end_timestamp={now_ts * 1000}"
        res = requests.get(url).json()
        closes = res.get('result', {}).get('c', []) 

        if len(closes) < 10:
            return 50.0 

        target_closes = closes[-10:]
        log_returns = np.diff(np.log(target_closes)) 

        daily_std = np.std(log_returns, ddof=1)
        return float(daily_std * math.sqrt(365) * 100.0)
    except Exception:
        return 50.0 

def background_data_worker(currency="BTC"):
    """Independent data collection engine running in a separate thread."""
    print("Background Upstash Processing Worker Loop Engaged.")
    while True:
        try:
            idx_url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={currency.lower()}_usd"
            idx_res = requests.get(idx_url).json()
            spot_price = float(idx_res['result']['index_price']) 

            opt_url = f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={currency}&kind=option"
            opt_res = requests.get(opt_url).json()
            data_list = opt_res['result']

            now = datetime.now(timezone.utc)
            parsed_options = []
            atm_iv = 50.0
            min_strike_dist = float('inf')

            for item in data_list:
                name = item['instrument_name']
                parts = name.split('-')
                if len(parts) < 4: continue 
                strike = float(parts[2])
                oi = float(item.get('open_interest', 0))
                if oi <= 0: continue
                expiry_str = parts[1]
                try:
                    expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc).replace(hour=8, minute=0, second=0)
                    days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
                    if days_to_expiry < 0: continue
                except Exception: continue
                
                iv = float(item.get('mark_iv', 50)) / 100.0
                if iv == 0: iv = 0.5 
                if days_to_expiry <= 7.0:
                    dist = abs(spot_price - strike)
                    if dist < min_strike_dist:
                        min_strike_dist = dist
                        atm_iv = iv * 100.0 

                parsed_options.append({'strike': strike, 'oi': oi, 'days_to_expiry': days_to_expiry})

            net_call_fiat_flow = 0.0
            net_put_fiat_flow = 0.0
            net_delta_premium_drift = 0.0

            call_ask_hit_premium = 0.0
            call_bid_hit_premium = 0.0
            put_ask_hit_premium = 0.0
            put_bid_hit_premium = 0.0 

            trades_url = f"https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency={currency}&kind=option&count=1000"
            trades_res = requests.get(trades_url).json()
            trades_list = trades_res.get('result', {}).get('trades', []) 

            last_processed_id = None
            try:
                last_logged_element = redis.lindex(REDIS_FLOW_KEY, -1)
                if last_logged_element:
                    last_processed_id = json.loads(last_logged_element).get("last_trade_id")
            except Exception as e:
                print(f"Error fetching last process state: {e}")

            incoming_ids = [str(t.get('trade_id', '')) for t in trades_list]
            
            slice_index = None
            if last_processed_id and last_processed_id in incoming_ids:
                slice_index = incoming_ids.index(last_processed_id)
                
            if slice_index is not None:
                active_trades_subset = trades_list[:slice_index]
            else:
                active_trades_subset = trades_list

            new_most_recent_id = str(trades_list[0].get('trade_id', '')) if trades_list else last_processed_id

            for trade in active_trades_subset:
                ins_name = trade.get('instrument_name', '')
                parts = ins_name.split('-')
                if len(parts) < 4: continue 
                expiry_str = parts[1]
                strike = float(parts[2])
                option_type = parts[3] 

                try:
                    expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc).replace(hour=8, minute=0)
                    days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
                except Exception: continue 

                direction = trade.get('direction', 'buy')
                amount = float(trade.get('amount', 0))
                trade_index_price = float(trade.get('index_price', spot_price))
                fiat_notional_value = amount * trade_index_price
                iv_trade = float(trade.get('iv', 50)) / 100.0 

                try:
                    t_trade = max(days_to_expiry, 0.01) / 365.0
                    d1_trade = (math.log(trade_index_price / strike) + (0.5 * (iv_trade ** 2)) * t_trade) / (iv_trade * math.sqrt(t_trade))
                    trade_delta = native_norm_cdf(d1_trade) if option_type == 'C' else (native_norm_cdf(d1_trade) - 1.0)
                except Exception: trade_delta = 0.5 if option_type == 'C' else -0.5 

                trade_ndf = trade_delta * fiat_notional_value
                if direction != 'buy': trade_ndf = -trade_ndf 
                net_delta_premium_drift += trade_ndf 

                if option_type == 'C':
                    if direction == 'buy':
                        net_call_fiat_flow += fiat_notional_value
                        call_ask_hit_premium += fiat_notional_value
                    else:
                        net_call_fiat_flow -= fiat_notional_value
                        call_bid_hit_premium += fiat_notional_value
                elif option_type == 'P':
                    if direction == 'buy':
                        net_put_fiat_flow += fiat_notional_value
                        put_ask_hit_premium += fiat_notional_value
                    else:
                        net_put_fiat_flow -= fiat_notional_value
                        put_bid_hit_premium += fiat_notional_value 

            current_ts = now.strftime("%m-%d %H:%M") 

            last_logged_element = redis.lindex(REDIS_FLOW_KEY, -1)
            is_duplicate = False
            if last_logged_element:
                if json.loads(last_logged_element).get("timestamp") == current_ts: is_duplicate = True
            if not is_duplicate:
                flow_snapshot = {
                    "timestamp": current_ts, 
                    "call_flow": round(net_call_fiat_flow, 2), 
                    "put_flow": round(net_put_fiat_flow, 2),
                    "ndf_drift": round(net_delta_premium_drift, 2), 
                    "c_ask": round(call_ask_hit_premium, 2), 
                    "c_bid": round(call_bid_hit_premium, 2),
                    "p_ask": round(put_ask_hit_premium, 2), 
                    "p_bid": round(put_bid_hit_premium, 2),
                    "last_trade_id": new_most_recent_id
                }
                redis.rpush(REDIS_FLOW_KEY, json.dumps(flow_snapshot))

            all_flow_records = redis.lrange(REDIS_FLOW_KEY, 0, -1)
            records_to_remove_count = 0
            for record in all_flow_records:
                f_data = json.loads(record)
                rec_time = datetime.strptime(f"{now.year}-{f_data['timestamp']}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                if rec_time > now: rec_time = rec_time.replace(year=now.year - 1)
                if (now - rec_time).total_seconds() > 86400.0: records_to_remove_count += 1
            if records_to_remove_count > 0: redis.ltrim(REDIS_FLOW_KEY, records_to_remove_count, -1)

            if now.minute <= 4:
                hourly_time_tag = now.strftime("%m-%d %H:%M")
                last_oi_element = redis.lindex(REDIS_OI_MIGRATION_KEY, -1)
                if last_oi_element and json.loads(last_oi_element).get("timestamp") == hourly_time_tag:
                    redis.rpop(REDIS_OI_MIGRATION_KEY)

                base_df = pd.DataFrame(parsed_options)
                oi_snapshot_map = {}
                if not base_df.empty:
                    base_df['strike_bucket'] = base_df['strike'].apply(lambda x: round(x / 500.0) * 500)
                    oi_snapshot_map = base_df.groupby('strike_bucket')['oi'].sum().to_dict()
                    oi_snapshot_map = {str(k): float(v) for k, v in oi_snapshot_map.items()} 

                oi_history_snapshot = {"timestamp": hourly_time_tag, "oi_distribution": oi_snapshot_map}
                redis.rpush(REDIS_OI_MIGRATION_KEY, json.dumps(oi_history_snapshot))
                redis.ltrim(REDIS_OI_MIGRATION_KEY, -168, -1)

            print("Background state sync complete.")
        except Exception as loop_ex:
            print(f"Background Loop Error encountered: {loop_ex}")
            
        time.sleep(300)

def fetch_deribit_gex(currency="BTC"):
    """Fetches and calculates current state visual metrics exclusively for chart render steps."""
    try:
        idx_url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={currency.lower()}_usd"
        idx_res = requests.get(idx_url).json()
        spot_price = float(idx_res['result']['index_price']) 

        opt_url = f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={currency}&kind=option"
        opt_res = requests.get(opt_url).json()
        data_list = opt_res['result']
    except Exception: return None 

    now = datetime.now(timezone.utc)
    parsed_options = []
    atm_iv = 50.0
    min_strike_dist = float('inf')

    net_speed_current = 0.0
    net_speed_down_1000 = 0.0
    net_speed_up_1000 = 0.0 

    for item in data_list:
        name = item['instrument_name']
        parts = name.split('-')
        if len(parts) < 4: continue 

        expiry_str = parts[1]
        strike = float(parts[2])
        option_type = parts[3]
        oi = float(item.get('open_interest', 0))
        volume = float(item.get('volume', 0)) 

        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc).replace(hour=8, minute=0, second=0)
            days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            if days_to_expiry < 0: continue
        except Exception: continue 

        iv = float(item.get('mark_iv', 50)) / 100.0
        if iv == 0: iv = 0.5 

        if days_to_expiry <= 7.0:
            dist = abs(spot_price - strike)
            if dist < min_strike_dist:
                min_strike_dist = dist
                atm_iv = iv * 100.0 

        try:
            t_days = max(days_to_expiry, 0.01) / 365.0
            distance = abs(math.log(spot_price / strike))
            approx_gamma = (1.0 / (iv * math.sqrt(t_days) * math.sqrt(2 * math.pi))) * math.exp(-0.5 * (distance / (iv * math.sqrt(t_days)))**2) / spot_price 

            d1 = (math.log(spot_price / strike) + (0.5 * (iv ** 2)) * t_days) / (iv * math.sqrt(t_days))
            d2 = d1 - iv * math.sqrt(t_days) 

            pdf_value = native_norm_pdf(d1)
            vanna_per_contract = -pdf_value * (d2 / iv)
            vanna_exposure_footprint = oi * vanna_per_contract * 0.01
            if option_type == 'P':
                vanna_exposure_footprint = -vanna_exposure_footprint
        except Exception:
            approx_gamma = 0.0001 / max(1.0, abs(spot_price - strike))
            vanna_exposure_footprint = 0.0 

        gex_value = oi * approx_gamma * (spot_price ** 2) * 0.01 
        if option_type == 'P':
            gex_value = -gex_value

        net_speed_current += calculate_speed_for_option(spot_price, strike, iv, days_to_expiry, oi, option_type)
        net_speed_down_1000 += calculate_speed_for_option(spot_price - 1000.0, strike, iv, days_to_expiry, oi, option_type)
        net_speed_up_1000 += calculate_speed_for_option(spot_price + 1000.0, strike, iv, days_to_expiry, oi, option_type) 

        parsed_options.append({
            'strike': strike, 'type': option_type, 'oi': oi, 'volume': volume,
            'gex': gex_value, 'vanna': vanna_exposure_footprint, 'iv': iv * 100.0, 'days_to_expiry': days_to_expiry
        }) 

    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None 

    df_1m = base_df[base_df['days_to_expiry'] <= 30.0]
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = df_1m 

    iv_shift_multiplier = 1.0
    if len(last_known_atm_iv) > 0:
        if atm_iv < last_known_atm_iv[-1]: iv_shift_multiplier = -1.0 
    last_known_atm_iv.append(atm_iv)
    if len(last_known_atm_iv) > 20: last_known_atm_iv.pop(0) 

    call_df_1m = df_1m[df_1m['type'] == 'C']
    put_df_1m = df_1m[df_1m['type'] == 'P']
    call_gex_1m = call_df_1m['gex'].sum()
    put_gex_1m = put_df_1m['gex'].sum()
    net_gex_1m = call_gex_1m + put_gex_1m
    total_abs_gex_1m = abs(call_gex_1m) + abs(put_gex_1m)
    call_weight_pct_1m = (abs(call_gex_1m) / total_abs_gex_1m * 100) if total_abs_gex_1m > 0 else 50.0 

    call_df_3d = df_3d[df_3d['type'] == 'C']
    put_df_3d = df_3d[df_3d['type'] == 'P']
    call_gex_3d = call_df_3d['gex'].sum()
    put_gex_3d = put_df_3d['gex'].sum()
    net_gex_3d = call_gex_3d + put_gex_3d
    total_abs_gex_3d = abs(call_gex_3d) + abs(put_gex_3d)
    call_weight_pct_3d = (abs(call_gex_3d) / total_abs_gex_3d * 100) if total_abs_gex_3d > 0 else 50.0 

    call_walls_3d = call_df_3d.groupby('strike')['gex'].sum().abs().sort_values(ascending=False).head(2)
    put_walls_3d = put_df_3d.groupby('strike')['gex'].sum().abs().sort_values(ascending=False).head(2) 

    c1_level = call_walls_3d.index[0] if len(call_walls_3d) >= 1 else spot_price
    c2_level = call_walls_3d.index[1] if len(call_walls_3d) >= 2 else spot_price
    p1_level = put_walls_3d.index[0] if len(put_walls_3d) >= 1 else spot_price
    p2_level = put_walls_3d.index[1] if len(put_walls_3d) >= 2 else spot_price 

    df_7d = base_df[base_df['days_to_expiry'] <= 7.0]
    skew_25d_val = 0.0
    if not df_7d.empty:
        calls_7d = df_7d[df_7d['type'] == 'C']
        puts_7d = df_7d[df_7d['type'] == 'P']
        
        c_idx = (calls_7d['strike'] - spot_price * 1.05).abs().idxmin() if not calls_7d.empty else None
        p_idx = (puts_7d['strike'] - spot_price * 0.95).abs().idxmin() if not puts_7d.empty else None
        
        if c_idx is not None and p_idx is not None:
            skew_25d_val = float(puts_7d.loc[p_idx, 'iv'] - calls_7d.loc[c_idx, 'iv'])

    strikes_3d = sorted(df_3d['strike'].unique())
    min_pain = float('inf')
    max_pain_level = spot_price
    for s in strikes_3d:
        pain = 0
        for _, row in df_3d.iterrows():
            if row['type'] == 'C' and row['strike'] < s: pain += (s - row['strike']) * row['oi']
            elif row['type'] == 'P' and row['strike'] > s: pain += (row['strike'] - s) * row['oi']
        if pain < min_pain: min_pain, max_pain_level = pain, s 

    df_3d_copy = df_3d.copy()
    df_3d_copy['macro_bucket'] = df_3d_copy['strike'].apply(lambda x: round(x / 500.0) * 500)
    macro_grouped = df_3d_copy.groupby('macro_bucket')['gex'].sum().sort_index() 

    flip_level = spot_price
    if not macro_grouped.empty:
        buckets_list = macro_grouped.index.tolist()
        for i in range(len(buckets_list) - 1):
            b1, b2 = buckets_list[i], buckets_list[i+1]
            g1, g2 = macro_grouped.loc[b1], macro_grouped.loc[b2]
            if (g1 < 0 and g2 > 0) or (g1 > 0 and g2 < 0):
                flip_level = round(b1 - g1 * (b2 - b1) / (g2 - g1))
                break 

    call_strike_gex_3d = call_df_3d.groupby('strike')['gex'].sum()
    put_strike_gex_3d = put_df_3d.groupby('strike')['gex'].sum().abs()
    resistance_level = call_strike_gex_3d.idxmax() if not call_strike_gex_3d.empty else spot_price * 1.02
    support_level = put_strike_gex_3d.idxmax() if not put_strike_gex_3d.empty else spot_price * 0.98
    breakout_price = resistance_level * 1.002 

    valid_flow_records = []
    try:
        all_flow_records = redis.lrange(REDIS_FLOW_KEY, 0, -1)
        for record in all_flow_records:
            valid_flow_records.append(json.loads(record))
    except Exception: pass

    total_accumulated_call_flow = sum(f["call_flow"] for f in valid_flow_records) if valid_flow_records else 0.0
    total_accumulated_put_flow = sum(f["put_flow"] for f in valid_flow_records) if valid_flow_records else 0.0
    net_flow_bias = total_accumulated_call_flow - total_accumulated_put_flow 
    total_cumulative_ndf_drift = sum(f.get("ndf_drift", 0.0) for f in valid_flow_records) if valid_flow_records else 0.0 

    total_c_ask = sum(f.get("c_ask", 0.0) for f in valid_flow_records) if valid_flow_records else 0.0
    total_c_bid = sum(f.get("c_bid", 0.0) for f in valid_flow_records) if valid_flow_records else 0.0
    total_p_ask = sum(f.get("p_ask", 0.0) for f in valid_flow_records) if valid_flow_records else 0.0
    total_p_bid = sum(f.get("p_bid", 0.0) for f in valid_flow_records) if valid_flow_records else 0.0

    center_spot_500 = round(spot_price / 500.0) * 500
    lower_bound = center_spot_500 - 6000
    upper_bound = center_spot_500 + 6000
    target_buckets = list(range(int(lower_bound), int(upper_bound) + 500, 500)) 

    df_chart_range_3d = df_3d[(df_3d['strike'] >= lower_bound) & (df_3d['strike'] <= upper_bound)].copy()
    df_chart_range_3d['strike_bucket'] = df_chart_range_3d['strike'].apply(lambda x: round(x / 500.0) * 500)
    bucket_data_3d = df_chart_range_3d.groupby('strike_bucket').agg({'gex': 'sum'}) 

    df_calls_3d = df_3d_copy[(df_3d_copy['type'] == 'C') & (df_3d_copy['strike'] >= lower_bound) & (df_3d_copy['strike'] <= upper_bound)]
    df_puts_3d = df_3d_copy[(df_3d_copy['type'] == 'P') & (df_3d_copy['strike'] >= lower_bound) & (df_3d_copy['strike'] <= upper_bound)]
    
    bucket_calls_3d = df_calls_3d.groupby('macro_bucket')['oi'].sum()
    bucket_puts_3d = df_puts_3d.groupby('macro_bucket')['oi'].sum()

    df_chart_range_1m = base_df[base_df['days_to_expiry'] <= 30.0][(base_df['strike'] >= lower_bound) & (base_df['strike'] <= upper_bound)].copy()
    df_chart_range_1m['strike_bucket'] = df_chart_range_1m['strike'].apply(lambda x: round(x / 500.0) * 500)
    bucket_data_1m = df_chart_range_1m.groupby('strike_bucket').agg({'gex': 'sum', 'vanna': 'sum', 'volume': 'sum', 'oi': 'sum'}) 

    df_7d_range = df_7d[(df_7d['strike'] >= lower_bound) & (df_7d['strike'] <= upper_bound)].copy() if not df_7d.empty else pd.DataFrame()
    bucket_iv_map = {}
    if not df_7d_range.empty:
        df_7d_range['strike_bucket'] = df_7d_range['strike'].apply(lambda x: round(x / 500.0) * 500)
        bucket_iv_map = df_7d_range.groupby('strike_bucket')['iv'].mean().to_dict() 

    chart_matrix = []
    for idx, b_strike in enumerate(target_buckets):
        gex_3d_val = bucket_data_3d['gex'].get(b_strike, 0.0) if b_strike in bucket_data_3d.index else 0.0
        gex_1m_val = bucket_data_1m['gex'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        vanna_val = bucket_data_1m['vanna'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        iv_skew_val = bucket_iv_map.get(b_strike, 0.0) 
        
        calls_oi_3d = bucket_calls_3d.get(b_strike, 0.0)
        puts_oi_3d = bucket_puts_3d.get(b_strike, 0.0)

        chart_matrix.append({
            "index": idx, "strike": b_strike,
            "gex_3d": gex_3d_val, "gex_1m": gex_1m_val,
            "vanna_exposure": vanna_val, "iv_skew": iv_skew_val,
            "calls_oi_3d": calls_oi_3d, "puts_oi_3d": puts_oi_3d
        }) 

    # DYNAMIC LOGIC POOL FOR EXPLORATION PROFILE MATRIX ENGINE
    total_oi_global = base_df['oi'].sum() if not base_df.empty else 1.0
    
    def calculate_expiry_slice_metrics(df, min_d, max_d):
        slice_df = df[(df['days_to_expiry'] >= min_d) & (df['days_to_expiry'] <= max_d)]
        if slice_df.empty:
            return 0.0, 1.0
        share = (slice_df['oi'].sum() / total_oi_global) * 100.0
        c_oi = slice_df[slice_df['type'] == 'C']['oi'].sum()
        p_oi = slice_df[slice_df['type'] == 'P']['oi'].sum()
        ratio = (c_oi / p_oi) if p_oi > 0 else (c_oi if c_oi > 0 else 1.0)
        return share, ratio

    s_0d, r_0d = calculate_expiry_slice_metrics(base_df, 0.0, 0.25) # Standard 0DTE slice threshold
    s_1d, r_1d = calculate_expiry_slice_metrics(base_df, 0.25, 1.1)
    s_3d, r_3d = calculate_expiry_slice_metrics(base_df, 1.1, 3.1)
    s_1w, r_1w = calculate_expiry_slice_metrics(base_df, 3.1, 7.1)
    s_1m, r_1m = calculate_expiry_slice_metrics(base_df, 7.1, 31.0)

    realized_vol_10d_val = calculate_realized_vol_10d(currency) 
    return {
        "spot": spot_price,
        "call_gex_1m": call_gex_1m, "put_gex_1m": put_gex_1m, "net_gex_1m": net_gex_1m, "call_weight_1m": call_weight_pct_1m,
        "call_gex_3d": call_gex_3d, "put_gex_3d": put_gex_3d, "net_gex_3d": net_gex_3d, "call_weight_3d": call_weight_pct_3d,
        "max_pain": max_pain_level, "flip": flip_level, "breakout": breakout_price,
        "resistance": resistance_level, "support": support_level, "call_inflow": total_accumulated_call_flow,
        "put_inflow": total_accumulated_put_flow, "net_flow": net_flow_bias, "chart_data": chart_matrix,
        "skew_25d": skew_25d_val, 
        "c1_wall": c1_level, "c2_wall": c2_level, "p1_wall": p1_level, "p2_wall": p2_level,
        "implied_vol": atm_iv, "realized_vol": realized_vol_10d_val,
        "ndf_drift_total": total_cumulative_ndf_drift,
        "aggr_call_ask": total_c_ask, "aggr_call_bid": total_c_bid,
        "aggr_put_ask": total_p_ask, "aggr_put_bid": total_p_bid,
        "speed_current": net_speed_current, "speed_down_1000": net_speed_down_1000, "speed_up_1000": net_speed_up_1000,
        "iv_direction": "EXPANDING" if iv_shift_multiplier > 0 else "CRUSHING",
        "raw_option_dataframe": bucket_data_1m,
        "expiry_profile": {
            "0d": (s_0d, r_0d), "1d": (s_1d, r_1d), "3d": (s_3d, r_3d), "1w": (s_1w, r_1w), "1m": (s_1m, r_1m)
        }
    } 

def fmt_gex(val):
    sign = "+" if val >= 0 else "-"
    abs_val = abs(val)
    return f"{sign}{abs_val/1000:.1f}k" if abs_val >= 1000 else f"{sign}{abs_val:.1f}" 

def fmt_signed_flow(val):
    sign = "+" if val > 0 else ""
    return f"{sign}{val / 1000000.0:,.1f}M" 

def main(page: ft.Page):
    page.title = "DERIBIT GEX DASHBOARD"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14 

    net_axis_3d = ft.ChartAxis(labels=[], labels_size=24)
    calls_axis_3d = ft.ChartAxis(labels=[], labels_size=24)
    puts_axis_3d = ft.ChartAxis(labels=[], labels_size=24)
    net_axis_1m = ft.ChartAxis(labels=[], labels_size=24)
    vanna_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    oi_migration_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_left_axis = ft.ChartAxis(labels=[], labels_size=42) 

    spot_price_container = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color="#b5d045") 

    call_gex_txt_1m = ft.Text("0.0k", size=14, weight=ft.FontWeight.W_600)
    put_gex_txt_1m = ft.Text("0.0k", size=14, weight=ft.FontWeight.W_600)
    net_gex_txt_1m = ft.Text("0.0k", size=14, weight=ft.FontWeight.BOLD)
    weight_txt_1m = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_300) 

    call_gex_txt_3d = ft.Text("0.0k", size=14, weight=ft.FontWeight.W_600)
    put_gex_txt_3d = ft.Text("0.0k", size=14, weight=ft.FontWeight.W_600)
    net_gex_txt_3d = ft.Text("0.0k", size=14, weight=ft.FontWeight.BOLD)
    weight_txt_3d = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600, color="#ab47bc") 

    skew_25d_txt = ft.Text("0.00% (Neutral)", size=14, weight=ft.FontWeight.BOLD) 

    pain_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600) 
    flip_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    breakout_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_ACCENT) 
    res_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_300)
    sup_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.PINK_400) 

    # EXPIRE ANALYSIS TEXT INSTANCES POOL
    p0_share = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    p0_ratio = ft.Text("1.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_GREY_300)
    p1_share = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    p1_ratio = ft.Text("1.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_GREY_300)
    p3_share = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    p3_ratio = ft.Text("1.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_GREY_300)
    pw_share = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    pw_ratio = ft.Text("1.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_GREY_300)
    pm_share = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    pm_ratio = ft.Text("1.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_GREY_300)

    inflows_call_txt = ft.Text("0.0M", size=14, weight=ft.FontWeight.W_600)
    outflows_put_txt = ft.Text("0.0M", size=14, weight=ft.FontWeight.W_600)
    net_flow_txt = ft.Text("0.0M", size=14, weight=ft.FontWeight.W_600) 

    call_ask_hit_txt = ft.Text("0.0M", size=14, color=ft.colors.GREEN_400)
    call_bid_hit_txt = ft.Text("0.0M", size=14, color=ft.colors.RED_400)
    put_ask_hit_txt = ft.Text("0.0M", size=14, color=ft.colors.RED_400)
    put_bid_hit_txt = ft.Text("0.0M", size=14, color=ft.colors.GREEN_400)
    aggr_net_bias_txt = ft.Text("0.0M", size=14, weight=ft.FontWeight.BOLD) 

    iv_metric_txt = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    rv_metric_txt = ft.Text("0.0%", size=14, weight=ft.FontWeight.W_600)
    vol_variance_txt = ft.Text("0.0% (Neutral)", size=14, weight=ft.FontWeight.BOLD) 

    grid_lines_config = ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5)

    speed_curr_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_down_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_up_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_regime_txt = ft.Text("Stable Neutral", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400) 

    ndf_drift_metric_txt = ft.Text("$0.0M", size=14, weight=ft.FontWeight.BOLD)
    ndf_structural_signal_txt = ft.Text("Neutral Absorption", size=14, weight=ft.FontWeight.BOLD) 

    gex_bar_chart_3d = ft.BarChart(bar_groups=[], bottom_axis=net_axis_3d,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240) 

    calls_oi_chart_3d = ft.BarChart(
        bar_groups=[], bottom_axis=calls_axis_3d,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240, min_y=0
    )

    puts_oi_chart_3d = ft.BarChart(
        bar_groups=[], bottom_axis=puts_axis_3d,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240, min_y=0
    )

    gex_bar_chart_1m = ft.BarChart(
        bar_groups=[], bottom_axis=net_axis_1m,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240) 

    vanna_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=vanna_bottom_axis,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240
    ) 

    oi_migration_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=oi_migration_bottom_axis,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240
    ) 

    id_skew_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=iv_bottom_axis, left_axis=iv_left_axis,
        horizontal_grid_lines=grid_lines_config, vertical_grid_lines=grid_lines_config,
        animate=True, interactive=True, height=240
    ) 

    def create_section_header(title):
        return ft.Container(content=ft.Text(title, size=13, weight=ft.FontWeight.BOLD, color=ft.colors.GREY_500), margin=ft.margin.only(top=15, bottom=5)) 

    def ui_row_item(label, component):
        return ft.Container(content=ft.Row([ft.Text(label, size=14, color=ft.colors.GREY_300), component], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=ft.padding.symmetric(vertical=4)) 

    def refresh_dashboard(e=None):
        m = fetch_deribit_gex("BTC")
        if m:
            spot_price_container.value = f"${m['spot']:,.2f}" 

            c_1m, p_1m = m['call_gex_1m'], m['put_gex_1m']
            if c_1m >= 0:
                call_gex_txt_1m.value = f"{fmt_gex(c_1m)} (Bearish)"
                call_gex_txt_1m.color = ft.colors.RED_400
            else:
                call_gex_txt_1m.value = f"{fmt_gex(c_1m)} (Bullish)"
                call_gex_txt_1m.color = ft.colors.GREEN_400 

            if p_1m >= 0:
                put_gex_txt_1m.value = f"{fmt_gex(p_1m)} (Bullish)"
                put_gex_txt_1m.color = ft.colors.GREEN_400
            else:
                put_gex_txt_1m.value = f"{fmt_gex(p_1m)} (Bearish)"
                put_gex_txt_1m.color = ft.colors.RED_400 

            net_gex_txt_1m.value = fmt_gex(m['net_gex_1m'])
            net_gex_txt_1m.color = ft.colors.GREEN_400 if m['net_gex_1m'] >= 0 else ft.colors.RED_400
            weight_txt_1m.value = f"{m['call_weight_1m']:.1f}%" 

            c_3d, p_3d, net_3d = m['call_gex_3d'], m['put_gex_3d'], m['net_gex_3d']
            if c_3d >= 0:
                call_gex_txt_3d.value = f"{fmt_gex(c_3d)} (Bearish)"
                call_gex_txt_3d.color = ft.colors.BLUE_400
            else:
                call_gex_txt_3d.value = f"{fmt_gex(c_3d)} (Bullish)"
                call_gex_txt_3d.color = ft.colors.ORANGE_400 

            if p_3d >= 0:
                put_gex_txt_3d.value = f"{fmt_gex(p_3d)} (Bullish)"
                put_gex_txt_3d.color = ft.colors.ORANGE_400
            else:
                put_gex_txt_3d.value = f"{fmt_gex(p_3d)} (Bearish)"
                put_gex_txt_3d.color = ft.colors.BLUE_400 

            net_gex_txt_3d.value = fmt_gex(net_3d)
            net_gex_txt_3d.color = ft.colors.ORANGE_400 if net_3d >= 0 else ft.colors.BLUE_400
            weight_txt_3d.value = f"{m['call_weight_3d']:.1f}%" 

            pain_txt.value = f"${m['max_pain']:,.0f}"
            flip_txt.value = f"${m['flip']:,.0f}"
            breakout_txt.value = f"${m['breakout']:,.0f}"
            res_txt.value = f"${m['resistance']:,.0f}"
            sup_txt.value = f"${m['support']:,.0f}" 

            # EXPIRY MATRIX WRITE BACK INTERPOLATION
            ep = m['expiry_profile']
            p0_share.value = f"{ep['0d'][0]:.2f}%"
            p0_ratio.value = f"{ep['0d'][1]:.2f}"
            p1_share.value = f"{ep['1d'][0]:.2f}%"
            p1_ratio.value = f"{ep['1d'][1]:.2f}"
            p3_share.value = f"{ep['3d'][0]:.2f}%"
            p3_ratio.value = f"{ep['3d'][1]:.2f}"
            pw_share.value = f"{ep['1w'][0]:.2f}%"
            pw_ratio.value = f"{ep['1w'][1]:.2f}"
            pm_share.value = f"{ep['1m'][0]:.2f}%"
            pm_ratio.value = f"{ep['1m'][1]:.2f}"

            sk_val = m['skew_25d']
            if sk_val > 0.5:
                skew_25d_txt.value = f"{sk_val:+.2f}% (Put Premium / Bearish Bias)"
                skew_25d_txt.color = ft.colors.RED_400
            elif sk_val < -0.5:
                skew_25d_txt.value = f"{sk_val:+.2f}% (Call Premium / Bullish Bias)"
                skew_25d_txt.color = ft.colors.GREEN_400
            else:
                skew_25d_txt.value = f"{sk_val:+.2f}% (Flat Vol Symmetrical)"
                skew_25d_txt.color = ft.colors.GREY_400

            c_flow, p_flow, net_bias = m['call_inflow'], m['put_inflow'], m['net_flow']
            inflows_call_txt.value = fmt_signed_flow(c_flow)
            if c_flow > 0: inflows_call_txt.color = ft.colors.GREEN_400
            elif c_flow < 0: inflows_call_txt.color = ft.colors.RED_400
            else: inflows_call_txt.color = ft.colors.GREY_400 

            outflows_put_txt.value = fmt_signed_flow(p_flow)
            if p_flow > 0: outflows_put_txt.color = ft.colors.RED_400
            elif p_flow < 0: outflows_put_txt.color = ft.colors.GREEN_400
            else: outflows_put_txt.color = ft.colors.GREY_400 

            net_flow_txt.value = fmt_signed_flow(net_bias)
            if net_bias > 0: net_flow_txt.color = ft.colors.GREEN_400
            elif net_bias < 0: net_flow_txt.color = ft.colors.RED_400
            else: net_flow_txt.color = ft.colors.GREY_400 

            c_ask, c_bid, p_ask, p_bid = m['aggr_call_ask'], m['aggr_call_bid'], m['aggr_put_ask'], m['aggr_put_bid']
            call_ask_hit_txt.value = f"{c_ask / 1000000.0:.1f}M"
            call_bid_hit_txt.value = f"{c_bid / 1000000.0:.1f}M"
            put_ask_hit_txt.value = f"{p_ask / 1000000.0:.1f}M"
            put_bid_hit_txt.value = f"{p_bid / 1000000.0:.1f}M" 

            net_aggr_premium = (c_ask + p_bid) - (c_bid + p_ask)
            aggr_net_bias_txt.value = fmt_signed_flow(net_aggr_premium)
            if net_aggr_premium > 0: aggr_net_bias_txt.color = ft.colors.GREEN_400
            elif net_aggr_premium < 0: aggr_net_bias_txt.color = ft.colors.RED_400
            else: aggr_net_bias_txt.color = ft.colors.GREY_400 

            iv_val, rv_val = m['implied_vol'], m['realized_vol']
            iv_metric_txt.value = f"{iv_val:.1f}%"
            rv_metric_txt.value = f"{rv_val:.1f}%" 

            variance_spread = iv_val - rv_val
            if variance_spread < 0:
                vol_variance_txt.value = f"{variance_spread:+.1f}% (Breakout Coming)"
                vol_variance_txt.color = ft.colors.GREEN_400
            else:
                vol_variance_txt.value = f"{variance_spread:+.1f}% (Sideways Risk)"
                vol_variance_txt.color = ft.colors.RED_400 

            sp_curr, sp_down, sp_up = m['speed_current'], m['speed_down_1000'], m['speed_up_1000']
            speed_curr_txt.value = f"{sp_curr:+.4f}"
            speed_down_txt.value = f"{sp_down:+.4f}"
            speed_up_txt.value = f"{sp_up:+.4f}" 

            if sp_down < sp_curr and sp_down < 0:
                speed_regime_txt.value = "CRITICAL: Downside Acceleration Risk (Waterfall Threat)"
                speed_regime_txt.color = ft.colors.RED_ACCENT
            elif sp_curr < -0.05:
                speed_regime_txt.value = "High Convexity Vulnerability Zone"
                speed_regime_txt.color = ft.colors.ORANGE_ACCENT
            else:
                speed_regime_txt.value = "Stable Net Gamma Profile"
                speed_regime_txt.color = ft.colors.GREEN_400 

            chart_drift_val = m['ndf_drift_total']
            ndf_drift_metric_txt.value = fmt_signed_flow(chart_drift_val)
            if chart_drift_val > 0:
                ndf_drift_metric_txt.color = ft.colors.GREEN_400
                ndf_structural_signal_txt.value = "Aggressive Delta Accumulation"
                ndf_structural_signal_txt.color = ft.colors.GREEN_400
            elif chart_drift_val < 0:
                ndf_drift_metric_txt.color = ft.colors.RED_400
                ndf_structural_signal_txt.value = "Persistent Delta Distribution"
                ndf_structural_signal_txt.color = ft.colors.RED_400
            else:
                ndf_drift_metric_txt.color = ft.colors.GREY_400
                ndf_structural_signal_txt.value = "Neutral Absorption"
                ndf_structural_signal_txt.color = ft.colors.GREY_400 

            historical_oi_deltas = {}
            try:
                oi_snapshots = redis.lrange(REDIS_OI_MIGRATION_KEY, -2, -1)
                if len(oi_snapshots) >= 2:
                    t0_data = json.loads(oi_snapshots[0]).get("oi_distribution", {})
                    t1_data = json.loads(oi_snapshots[1]).get("oi_distribution", {}) 
                    for k_strike in t1_data.keys():
                        historical_oi_deltas[float(k_strike)] = float(t1_data[k_strike]) - float(t0_data.get(k_strike, 0.0))
            except Exception: pass 

            groups_net_3d = []
            groups_calls_3d = []
            groups_puts_3d = []
            groups_net_1m = []
            groups_vanna = []
            groups_oi_migration = []
            iv_bar_groups = []
            new_labels = []
            
            min_dist = float('inf')
            spot_index = -1 

            for item in m['chart_data']:
                dist = abs(item['strike'] - m['spot'])
                if dist < min_dist: 
                    min_dist = dist
                    spot_index = item['index'] 

            max_abs_vanna_exposure = 0.0001
            max_abs_oi_delta = 0.0001 
            max_short_term_oi_in_view = 0.0001

            for item in m['chart_data']:
                if abs(item['vanna_exposure']) > max_abs_vanna_exposure: max_abs_vanna_exposure = abs(item['vanna_exposure']) 
                stk = item['strike']
                oi_change = historical_oi_deltas.get(stk, 0.0)
                if abs(oi_change) > max_abs_oi_delta: max_abs_oi_delta = abs(oi_change) 
                
                if item['calls_oi_3d'] > max_short_term_oi_in_view: max_short_term_oi_in_view = item['calls_oi_3d']
                if item['puts_oi_3d'] > max_short_term_oi_in_view: max_short_term_oi_in_view = item['puts_oi_3d']

            aligned_oi_bound = max_short_term_oi_in_view * 1.15
            calls_oi_chart_3d.max_y = aligned_oi_bound
            puts_oi_chart_3d.max_y = aligned_oi_bound

            vanna_exposure_bound = max_abs_vanna_exposure * 1.15
            vanna_bar_chart.min_y = -vanna_exposure_bound
            vanna_bar_chart.max_y = vanna_exposure_bound 

            oi_migration_bound = max_abs_oi_delta * 1.15
            oi_migration_bar_chart.min_y = -oi_migration_bound
            oi_migration_bar_chart.max_y = oi_migration_bound 

            for item in m['chart_data']:
                strike_val = item['strike']
                is_spot = (item['index'] == spot_index)
                val_3d = item['gex_3d']
                val_1m = item['gex_1m']
                v_exposure = item['vanna_exposure']
                iv_val_item = item['iv_skew']
                
                c_oi_3d = item['calls_oi_3d']
                p_oi_3d = item['puts_oi_3d']

                groups_net_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_3d, color="#0cd56e" if val_3d >= 0 else "#e91841", width=6, border_radius=1)]))
                groups_calls_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=c_oi_3d, color="#0cd56e", width=6, border_radius=1)]))
                groups_puts_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=p_oi_3d, color="#e91841", width=6, border_radius=1)]))
                groups_net_1m.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_1m, color="#bab7ab" if val_1m >= 0 else "#1661b4", width=6, border_radius=1)]))
                groups_vanna.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=v_exposure, color="#d26e5a" if v_exposure >= 0 else ft.colors.WHITE70, width=6, border_radius=1)])) 

                oi_delta = historical_oi_deltas.get(strike_val, 0.0)
                groups_oi_migration.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=oi_delta, color="#35c2b3" if oi_delta >= 0 else "#7948be", width=6, border_radius=1)])) 

                valid_ivs = [it['iv_skew'] for it in m['chart_data'] if it['iv_skew'] > 0]
                max_iv_val = max(valid_ivs) if valid_ivs else 100.0
                min_iv_val = min(valid_ivs) if valid_ivs else 0.0 
                floor_y = math.floor(min_iv_val / 10.0) * 10.0
                ceil_y = math.ceil(max_iv_val / 10.0) * 10.0
                if ceil_y == floor_y: ceil_y += 10.0 
                id_skew_bar_chart.min_y = floor_y
                id_skew_bar_chart.max_y = ceil_y 

                iv_bar_groups.append(ft.BarChartGroup(
                    x=item['index'],
                    bar_rods=[ft.BarChartRod(from_y=floor_y, to_y=iv_val_item if iv_val_item > 0 else floor_y, color=ft.colors.ORANGE_700, width=6, border_radius=1)]
                ))
                
                if strike_val % 1000 == 0:
                    label_color = ft.colors.BLUE_200 if is_spot else ft.colors.GREY_400
                    new_labels.append(ft.ChartAxisLabel(value=item['index'], label=ft.Text(f"{strike_val/1000:.0f}k", size=10, color=label_color, rotate=45, weight=ft.FontWeight.BOLD if is_spot else ft.FontWeight.NORMAL)))
            
            gex_bar_chart_3d.bar_groups = groups_net_3d
            net_axis_3d.labels = new_labels
            calls_oi_chart_3d.bar_groups = groups_calls_3d
            calls_axis_3d.labels = list(new_labels)
            puts_oi_chart_3d.bar_groups = groups_puts_3d
            puts_axis_3d.labels = list(new_labels)
            gex_bar_chart_1m.bar_groups = groups_net_1m
            net_axis_1m.labels = list(new_labels)
            vanna_bar_chart.bar_groups = groups_vanna
            vanna_bottom_axis.labels = list(new_labels)
            oi_migration_bar_chart.bar_groups = groups_oi_migration
            oi_migration_bottom_axis.labels = list(new_labels)
            id_skew_bar_chart.bar_groups = iv_bar_groups
            iv_bottom_axis.labels = list(new_labels)
            
            page.update()

    page.add(
        ft.Row([ft.Text("DERIBIT GEX DASHBOARD", size=20, weight=ft.FontWeight.BOLD)], alignment=ft.MainAxisAlignment.START),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("Bitcoin Spot Price", size=11, color=ft.colors.GREY_500), spot_price_container], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
        create_section_header("NET GAMMA EXPOSURE BY STRIKE (3D)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart_3d)),

        create_section_header("CALL OPTIONS DISTRIBUTION (3D)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=calls_oi_chart_3d)),

        create_section_header("PUT OPTIONS DISTRIBUTION (3D)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=puts_oi_chart_3d)),

        create_section_header("IMPORTANT LEVELS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Max Pain", pain_txt), ui_row_item("Flip Zone", flip_txt), ui_row_item("Breakout Price", breakout_txt), ui_row_item("Resistance Level", res_txt), ui_row_item("Support Level", sup_txt)]))),

        # NEW RESTRUCTURED: EXPIRATION TIMELINE COMPOSITION MATRIX
        create_section_header("OPTIONS EXPIRATION TIME HORIZON ANALYSIS"),
        ft.Card(content=ft.Container(padding=14, content=ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("", size=13, color=ft.colors.GREY_400)),
                ft.DataColumn(ft.Text("Share", size=13, color=ft.colors.GREY_400)),
                ft.DataColumn(ft.Text("C/P", size=13, color=ft.colors.GREY_400)),
            ],
            rows=[
                ft.DataRow(cells=[ft.DataCell(ft.Text("0DTE", weight=ft.FontWeight.BOLD)), ft.DataCell(p0_share), ft.DataCell(p0_ratio)]),
                ft.DataRow(cells=[ft.DataCell(ft.Text("1DTE", weight=ft.FontWeight.BOLD)), ft.DataCell(p1_share), ft.DataCell(p1_ratio)]),
                ft.DataRow(cells=[ft.DataCell(ft.Text("3DTE", weight=ft.FontWeight.BOLD)), ft.DataCell(p3_share), ft.DataCell(p3_ratio)]),
                ft.DataRow(cells=[ft.DataCell(ft.Text("1WTE", weight=ft.FontWeight.BOLD)), ft.DataCell(pw_share), ft.DataCell(pw_ratio)]),
                ft.DataRow(cells=[ft.DataCell(ft.Text("1MTE", weight=ft.FontWeight.BOLD)), ft.DataCell(pm_share), ft.DataCell(pm_ratio)]),
            ]
        ))),

        create_section_header("NET GAMMA EXPOSURE BY STRIKE (1M)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart_1m)),

        create_section_header("NET VANNA EXPOSURE PROFILE (VEX)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=vanna_bar_chart)),

        create_section_header("OPEN INTEREST MIGRATION ENGINE (HOURLY DELTA)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=oi_migration_bar_chart)),

        create_section_header("TOTAL GAMMA EXPOSURE (1M)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Call Gamma", call_gex_txt_1m), ui_row_item("Put Gamma", put_gex_txt_1m), ui_row_item("Net Gamma", net_gex_txt_1m), ui_row_item("Call Weight (%)", weight_txt_1m)
        ]))),
        create_section_header("TOTAL GAMMA EXPOSURE (3D)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Call Gamma", call_gex_txt_3d), ui_row_item("Put Gamma", put_gex_txt_3d), ui_row_item("Net Gamma", net_gex_txt_3d), ui_row_item("Call Weight (%)", weight_txt_3d)
        ]))),
        
        create_section_header("IV SKEW ANALYSIS (7D)"),
        ft.Card(content=ft.Container(padding=15, content=ft.Column([
            id_skew_bar_chart,
            ft.Container(height=10),
            ui_row_item("25D Skew", skew_25d_txt)
        ]))),
        
        create_section_header("24H ACCUMULATED ORDER FLOW ANALYSIS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Net Call Inflows", inflows_call_txt), 
            ui_row_item("Net Put Inflows", outflows_put_txt), 
            ui_row_item("Net Premium Bias", net_flow_txt)
        ]))),

        create_section_header("BLOCK TRADE AGGRESSOR ANALYSIS (BID vs ASK)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Call Aggressor Buys (Hit Ask)", call_ask_hit_txt),
            ui_row_item("Call Aggressor Sells (Hit Bid)", call_bid_hit_txt),
            ui_row_item("Put Aggressor Buys (Hit Ask)", put_ask_hit_txt),
            ui_row_item("Put Aggressor Sells (Hit Bid)", put_bid_hit_txt),
            ui_row_item("Net Aggressor Premium Bias", aggr_net_bias_txt)
        ]))),

        create_section_header("VOLATILITY VARIANCE ANALYSIS (10D)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Implied Volatility (IV)", iv_metric_txt),
            ui_row_item("Realized Volatility (RV)", rv_metric_txt),
            ui_row_item("IV - RV Variation", vol_variance_txt)
        ]))),

        create_section_header("DEALER SPOT GAMMA ACCELERATION (SPEED)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Current Spot Speed Engine", speed_curr_txt),
            ui_row_item("Predictive Stress: Spot -$1000 Slippage", speed_down_txt),
            ui_row_item("Predictive Stress: Spot +$1000 Rally", speed_up_txt),
            ft.Divider(height=10, color=ft.colors.GREY_800),
            ui_row_item("Regime", speed_regime_txt)
        ]))),

        create_section_header("CUMULATIVE DELTA PREMIUM DRIFT (NDF)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("24H Net Delta Flow Drift", ndf_drift_metric_txt),
            ui_row_item("Tape", ndf_structural_signal_txt)
        ])))
    )
    refresh_dashboard()

if __name__ == "__main__":
    worker_thread = threading.Thread(target=background_data_worker, daemon=True)
    worker_thread.start()
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=None)
