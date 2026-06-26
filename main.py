import os
import math
import json
import requests
import pandas as pd
import flet as ft
from datetime import datetime, timezone, timedelta

# Initialize Upstash Redis with exact working credentials configuration
from upstash_redis import Redis
redis = Redis(
    url="https://large-ghost-131173.upstash.io", 
    token="gQAAAAAAAgBlAAIgcDE2NmI0NGZkNDFiYTk0TzlhOWJmZGM1MTg5OWViZDIxMw"
)
REDIS_KEY = "deribit_gex_3d_history"
REDIS_FLOW_KEY = "deribit_flow_24h_history"
MAX_HISTORY_POINTS = 3500

def fetch_deribit_gex(currency="BTC"):
    """Fetches and calculates GEX and market flow data from Deribit."""
    try:
        idx_url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={currency.lower()}_usd"
        idx_res = requests.get(idx_url).json()
        spot_price = float(idx_res['result']['index_price'])
        
        opt_url = f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency={currency}&kind=option"
        opt_res = requests.get(opt_url).json()
        data_list = opt_res['result']
    except Exception:
        return None

    now = datetime.now(timezone.utc)
    parsed_options = []
    
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
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            expiry_dt = expiry_dt.replace(hour=8, minute=0, second=0)
            days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            if days_to_expiry < 0: continue
        except Exception:
            continue
            
        iv = float(item.get('mark_iv', 50)) / 100.0
        if iv == 0: iv = 0.5
            
        try:
            t_days = max(days_to_expiry, 0.01) / 365.0
            distance = abs(math.log(spot_price / strike))
            approx_gamma = (1.0 / (iv * math.sqrt(t_days) * math.sqrt(2 * math.pi))) * math.exp(-0.5 * (distance / (iv * math.sqrt(t_days)))**2) / spot_price
        except Exception:
            approx_gamma = 0.0001 / max(1.0, abs(spot_price - strike))

        gex_value = oi * approx_gamma * (spot_price ** 2) * 0.01
        if option_type == 'P':
            gex_value = -gex_value
            
        parsed_options.append({
            'strike': strike, 
            'type': option_type, 
            'oi': oi, 
            'volume': volume, 
            'gex': gex_value,
            'days_to_expiry': days_to_expiry
        })
        
    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None
    
    df_3m = base_df[base_df['days_to_expiry'] <= 90.0]
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = df_3m

    call_df_3m = df_3m[df_3m['type'] == 'C']
    put_df_3m = df_3m[df_3m['type'] == 'P']
    
    call_gex = call_df_3m['gex'].sum()
    put_gex = put_df_3m['gex'].sum()
    net_gex = call_gex + put_gex
    net_gex_3m = net_gex
    
    total_abs_gex = abs(call_gex) + abs(put_gex)
    call_weight_pct = (abs(call_gex) / total_abs_gex * 100) if total_abs_gex > 0 else 50.0
    
    call_df_3d = df_3d[df_3d['type'] == 'C']
    put_df_3d = df_3d[df_3d['type'] == 'P']
    
    strikes_3d = sorted(df_3d['strike'].unique())
    min_pain = float('inf')
    max_pain_level = spot_price
    
    for s in strikes_3d:
        pain = 0
        for _, row in df_3d.iterrows():
            if row['type'] == 'C' and row['strike'] < s: 
                pain += (s - row['strike']) * row['oi']
            elif row['type'] == 'P' and row['strike'] > s: 
                pain += (row['strike'] - s) * row['oi']
        if pain < min_pain:
            min_pain = pain
            max_pain_level = s

    df_3d_copy = df_3d.copy()
    df_3d_copy['macro_bucket'] = df_3d_copy['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    macro_grouped = df_3d_copy.groupby('macro_bucket')['gex'].sum().sort_index()

    flip_level = spot_price
    if not macro_grouped.empty:
        buckets_list = macro_grouped.index.tolist()
        for i in range(len(buckets_list) - 1):
            b1, b2 = buckets_list[i], buckets_list[i+1]
            g1, g2 = macro_grouped.loc[b1], macro_grouped.loc[b2]
            
            if (g1 < 0 and g2 > 0) or (g1 > 0 and g2 < 0):
                flip_level = b1 - g1 * (b2 - b1) / (g2 - g1)
                flip_level = round(flip_level)
                break

    call_strike_gex_3d = call_df_3d.groupby('strike')['gex'].sum()
    put_strike_gex_3d = put_df_3d.groupby('strike')['gex'].sum().abs()
    
    resistance_level = call_strike_gex_3d.idxmax() if not call_strike_gex_3d.empty else spot_price * 1.02
    support_level = put_strike_gex_3d.idxmax() if not put_strike_gex_3d.empty else spot_price * 0.98
    breakout_price = resistance_level * 1.002

    # --- LIVE OPTION TAPE LOGIC ---
    net_call_fiat_flow = 0.0
    net_put_fiat_flow = 0.0
    try:
        trades_url = f"https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency={currency}&kind=option&count=1000"
        trades_res = requests.get(trades_url).json()
        trades_list = trades_res.get('result', {}).get('trades', [])
        
        for trade in trades_list:
            ins_name = trade.get('instrument_name', '')
            direction = trade.get('direction', 'buy')
            amount = float(trade.get('amount', 0))
            trade_index_price = float(trade.get('index_price', spot_price))
            
            fiat_notional_value = amount * trade_index_price
            
            if ins_name.endswith('-C'):
                if direction == 'buy': net_call_fiat_flow += fiat_notional_value
                else: net_call_fiat_flow -= fiat_notional_value
            elif ins_name.endswith('-P'):
                if direction == 'buy': net_put_fiat_flow -= fiat_notional_value
                else: net_put_fiat_flow += fiat_notional_value
    except Exception as ex:
        print(f"Option Tape Fetch Interrupted: {ex}")

    time_now = datetime.now(timezone.utc)
    current_ts = time_now.strftime("%m-%d %H:%M")

    # --- TIME-BASED HISTORICAL LOG CLEAN-UP ENGINE ---
    try:
        last_logged_element = redis.lindex(REDIS_FLOW_KEY, -1)
        is_duplicate = False
        if last_logged_element:
            last_logged_data = json.loads(last_logged_element)
            if last_logged_data.get("timestamp") == current_ts:
                is_duplicate = True

        if not is_duplicate:
            flow_snapshot = {
                "timestamp": current_ts,
                "call_flow": round(net_call_fiat_flow, 2),
                "put_flow": round(net_put_fiat_flow, 2)
            }
            redis.rpush(REDIS_FLOW_KEY, json.dumps(flow_snapshot))

        all_flow_records = redis.lrange(REDIS_FLOW_KEY, 0, -1)
        valid_flow_records = []
        records_to_remove_count = 0

        for record in all_flow_records:
            f_data = json.loads(record)
            ts_str = f_data['timestamp']
            rec_time = datetime.strptime(f"{time_now.year}-{ts_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if rec_time > time_now: rec_time = rec_time.replace(year=time_now.year - 1)
            
            if (time_now - rec_time).total_seconds() <= 86400.0:
                valid_flow_records.append(f_data)
            else:
                records_to_remove_count += 1

        if records_to_remove_count > 0:
            redis.ltrim(REDIS_FLOW_KEY, records_to_remove_count, -1)

    except Exception as ex:
        print(f"Cloud Flow Lifecycle Eviction Guard Triggered: {ex}")
        valid_flow_records = []

    total_accumulated_call_flow = 0.0
    total_accumulated_put_flow = 0.0

    if valid_flow_records:
        for f_data in valid_flow_records:
            total_accumulated_call_flow += f_data["call_flow"]
            total_accumulated_put_flow += f_data["put_flow"]
    else:
        total_accumulated_call_flow = net_call_fiat_flow
        total_accumulated_put_flow = net_put_fiat_flow

    net_flow_bias = total_accumulated_call_flow + total_accumulated_put_flow
    call_oi_3m = call_df_3m['oi'].sum()
    put_
