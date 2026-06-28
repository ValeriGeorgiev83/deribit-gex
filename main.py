import os
import math
import json
import requests
import pandas as pd
import numpy as np
import flet as ft
from datetime import datetime, timezone, timedelta

# Initialize Upstash Redis with your EXACT verified connection parameters
from upstash_redis import Redis
redis = Redis(
    url="https://large-ghost-131173.upstash.io", 
    token="gQAAAAAAAgBlAAIgcDE2NmI0NGZkNDFiYTk0TzlhOWJmZGM1MTg5OWViZDIxMw"
)
REDIS_FLOW_KEY = "deribit_flow_24h_history"
REDIS_WHALE_KEY = "deribit_whale_blocks_24h"
REDIS_OI_MIGRATION_KEY = "deribit_oi_hourly_history"
WHALE_THRESHOLD_USD = 500000.0

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

def fetch_deribit_gex(currency="BTC"):
    """Fetches and calculates GEX, flows, delta drifts, whale blocks, IV/RV, Speed, Charm, Vanna, and forward rehedge predictive estimators."""
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
    atm_iv = 50.0
    min_strike_dist = float('inf')
    net_charm_accumulator = 0.0
    
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
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            expiry_dt = expiry_dt.replace(hour=8, minute=0, second=0)
            days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            if days_to_expiry < 0: continue
        except Exception:
            continue
            
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
            if option_type == 'C':
                charm_per_contract = -pdf_value * ((0.0) / (iv * math.sqrt(t_days)) - d2 / (2 * t_days))
            else:
                charm_per_contract = pdf_value * ((0.0) / (iv * math.sqrt(t_days)) + d2 / (2 * t_days))
                
            charm_day_footprint = charm_per_contract / 365.0
            
            vanna_per_contract = -pdf_value * (d2 / iv)
            vanna_exposure_footprint = oi * vanna_per_contract * 0.01
            if option_type == 'P':
                vanna_exposure_footprint = -vanna_exposure_footprint
        except Exception:
            approx_gamma = 0.0001 / max(1.0, abs(spot_price - strike))
            charm_day_footprint = 0.0
            vanna_exposure_footprint = 0.0

        gex_value = oi * approx_gamma * (spot_price ** 2) * 0.01
        
        item_charm_exposure = oi * charm_day_footprint
        if option_type == 'P':
            gex_value = -gex_value
            item_charm_exposure = -item_charm_exposure
            
        net_charm_accumulator += item_charm_exposure
        
        net_speed_current += calculate_speed_for_option(spot_price, strike, iv, days_to_expiry, oi, option_type)
        net_speed_down_1000 += calculate_speed_for_option(spot_price - 1000.0, strike, iv, days_to_expiry, oi, option_type)
        net_speed_up_1000 += calculate_speed_for_option(spot_price + 1000.0, strike, iv, days_to_expiry, oi, option_type)
        
        parsed_options.append({
            'strike': strike, 
            'type': option_type, 
            'oi': oi, 
            'volume': volume, 
            'gex': gex_value,
            'vanna': vanna_exposure_footprint,
            'iv': iv * 100.0, 
            'days_to_expiry': days_to_expiry
        })
        
    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None
    
    df_1m = base_df[base_df['days_to_expiry'] <= 30.0]
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = df_1m

    # Calculate live IV shift direction multiplier
    iv_shift_multiplier = 1.0
    if len(last_known_atm_iv) > 0:
        if atm_iv < last_known_atm_iv[-1]:
            iv_shift_multiplier = -1.0  # Volatility is crushing down
    last_known_atm_iv.append(atm_iv)
    if len(last_known_atm_iv) > 20: last_known_atm_iv.pop(0)

    # --- GEX MATRICES ---
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
        c_25d = calls_7d.loc[(calls_7d['strike'] - spot_price * 1.05).abs().idxmin()] if not calls_7d.empty else None
        p_25d = puts_7d.loc[(puts_7d['strike'] - spot_price * 0.95).abs().idxmin()] if not puts_7d.empty else None
        if c_25d is not None and p_25d is not None:
            skew_25d_val = p_25d['iv'] - c_25d['iv']

    strikes_3d = sorted(df_3d['strike'].unique())
    min_pain = float('inf')
    max_pain_level = spot_price
    for s in strikes_3d:
        pain = 0
        for _, row in df_3d.iterrows():
            if row['type'] == 'C' and row['strike'] < s: pain += (s - row['strike']) * row['oi']
            elif row['type'] == 'P' and row['strike'] > s: pain += (row['strike'] - s) * row['oi']
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

    # --- LIVE OPTION TAPE LOGIC + WHALE BLOCK FILTERING ---
    net_call_fiat_flow = 0.0
    net_put_fiat_flow = 0.0
    net_delta_premium_drift = 0.0
    detected_whale_blocks = []
    
    call_ask_hit_premium = 0.0
    call_bid_hit_premium = 0.0
    put_ask_hit_premium = 0.0
    put_bid_hit_premium = 0.0
    
    try:
        trades_url = f"https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency={currency}&kind=option&count=1000"
        trades_res = requests.get(trades_url).json()
        trades_list = trades_res.get('result', {}).get('trades', [])
        
        for trade in trades_list:
            ins_name = trade.get('instrument_name', '')
            parts = ins_name.split('-')
            if len(parts) < 4: continue
            
            expiry_str = parts[1]
            strike = float(parts[2])
            option_type = parts[3]
            
            try:
                expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc).replace(hour=8, minute=0)
                days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            except Exception:
                continue
                
            direction = trade.get('direction', 'buy')
            amount = float(trade.get('amount', 0))
            trade_index_price = float(trade.get('index_price', spot_price))
            fiat_notional_value = amount * trade_index_price
            trade_id = str(trade.get('trade_id', ''))
            timestamp_ms = trade.get('timestamp', int(now.timestamp() * 1000))
            iv_trade = float(trade.get('iv', 50)) / 100.0

            try:
                t_trade = max(days_to_expiry, 0.01) / 365.0
                d1_trade = (math.log(trade_index_price / strike) + (0.5 * (iv_trade ** 2)) * t_trade) / (iv_trade * math.sqrt(t_trade))
                trade_delta = native_norm_cdf(d1_trade) if option_type == 'C' else (native_norm_cdf(d1_trade) - 1.0)
            except Exception:
                trade_delta = 0.5 if option_type == 'C' else -0.5

            trade_ndf = trade_delta * fiat_notional_value
            if direction != 'buy':
                trade_ndf = -trade_ndf

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
                
            if days_to_expiry <= 3.0 and fiat_notional_value >= WHALE_THRESHOLD_USD:
                detected_whale_blocks.append({
                    "trade_id": trade_id,
                    "timestamp_epoch": timestamp_ms / 1000.0,
                    "strike": strike,
                    "type": option_type,
                    "direction": direction,
                    "fiat_value": fiat_notional_value
                })
    except Exception as ex:
        print(f"Option Tape Fetch Interrupted: {ex}")

    time_now = datetime.now(timezone.utc)
    current_ts = time_now.strftime("%m-%d %H:%M")

    try:
        last_logged_element = redis.lindex(REDIS_FLOW_KEY, -1)
        is_duplicate = False
        if last_logged_element:
            last_logged_data = json.loads(last_logged_element)
            if last_logged_data.get("timestamp") == current_ts: is_duplicate = True
        if not is_duplicate:
            flow_snapshot = {
                "timestamp": current_ts, 
                "call_flow": round(net_call_fiat_flow, 2), 
                "put_flow": round(net_put_fiat_flow, 2),
                "ndf_drift": round(net_delta_premium_drift, 2)
            }
            redis.rpush(REDIS_FLOW_KEY, json.dumps(flow_snapshot))
        all_flow_records = redis.lrange(REDIS_FLOW_KEY, 0, -1)
        valid_flow_records, records_to_remove_count = [], 0
        for record in all_flow_records:
            f_data = json.loads(record)
            rec_time = datetime.strptime(f"{time_now.year}-{f_data['timestamp']}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if rec_time > time_now: rec_time = rec_time.replace(year=time_now.year - 1)
            if (time_now - rec_time).total_seconds() <= 86400.0: valid_flow_records.append(f_data)
            else: records_to_remove_count += 1
        if records_to_remove_count > 0: redis.ltrim(REDIS_FLOW_KEY, records_to_remove_count, -1)
    except Exception as ex:
        print(f"Flow Eviction Fail: {ex}")
        valid_flow_records = []

    total_accumulated_call_flow = sum(f["call_flow"] for f in valid_flow_records) if valid_flow_records else net_call_fiat_flow
    total_accumulated_put_flow = sum(f["put_flow"] for f in valid_flow_records) if valid_flow_records else net_put_fiat_flow
    net_flow_bias = total_accumulated_call_flow - total_accumulated_put_flow
    
    total_cumulative_ndf_drift = sum(f.get("ndf_drift", 0.0) for f in valid_flow_records) if valid_flow_records else net_delta_premium_drift

    try:
        if detected_whale_blocks:
            existing_whale_records = redis.lrange(REDIS_WHALE_KEY, 0, -1)
            known_ids = set()
            for r in existing_whale_records:
                try: known_ids.add(json.loads(r)["trade_id"])
                except Exception: pass
            
            for block in detected_whale_blocks:
                if block["trade_id"] not in known_ids:
                    redis.rpush(REDIS_WHALE_KEY, json.dumps(block))

        all_whale_records = redis.lrange(REDIS_WHALE_KEY, 0, -1)
        valid_whale_blocks, whale_remove_count = [], 0
        current_time_epoch = time_now.timestamp()
        
        for r in all_whale_records:
            b_data = json.loads(r)
            if (current_time_epoch - b_data["timestamp_epoch"]) <= 86400.0:
                valid_whale_blocks.append(b_data)
            else:
                whale_remove_count += 1
        if whale_remove_count > 0:
            redis.ltrim(REDIS_WHALE_KEY, whale_remove_count, -1)
    except Exception as ex:
        print(f"Cloud Whale Engine Interrupt: {ex}")
        valid_whale_blocks = []

    center_spot_1k = round(spot_price / 1000.0) * 1000
    lower_bound = center_spot_1k - 8000
    upper_bound = center_spot_1k + 8000
    target_buckets = list(range(int(lower_bound), int(upper_bound) + 1000, 1000))

    whale_matrix = {b: {"bullish": 0.0, "bearish": 0.0} for b in target_buckets}
    for b_trade in valid_whale_blocks:
        rounded_strike = round(b_trade["strike"] / 1000.0) * 1000
        if rounded_strike in whale_matrix:
            opt_type = b_trade["type"]
            side = b_trade["direction"]
            val = b_trade["fiat_value"]
            if (opt_type == "C" and side == "buy") or (opt_type == "P" and side == "sell"):
                whale_matrix[rounded_strike]["bullish"] += val
            elif (opt_type == "C" and side == "sell") or (opt_type == "P" and side == "buy"):
                whale_matrix[rounded_strike]["bearish"] += val

    df_chart_range_3d = df_3d[(df_3d['strike'] >= lower_bound) & (df_3d['strike'] <= upper_bound)].copy()
    df_chart_range_3d['strike_bucket'] = df_chart_range_3d['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    bucket_data_3d = df_chart_range_3d.groupby('strike_bucket').agg({'gex': 'sum'})

    df_chart_range_1m = base_df[base_df['days_to_expiry'] <= 30.0][(base_df['strike'] >= lower_bound) & (base_df['strike'] <= upper_bound)].copy()
    df_chart_range_1m['strike_bucket'] = df_chart_range_1m['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    bucket_data_1m = df_chart_range_1m.groupby('strike_bucket').agg({'gex': 'sum', 'vanna': 'sum', 'volume': 'sum', 'oi': 'sum'})
    
    df_7d_range = df_7d[(df_7d['strike'] >= lower_bound) & (df_7d['strike'] <= upper_bound)].copy() if not df_7d.empty else pd.DataFrame()
    bucket_iv_map = {}
    if not df_7d_range.empty:
        df_7d_range['strike_bucket'] = df_7d_range['strike'].apply(lambda x: round(x / 1000.0) * 1000)
        bucket_iv_map = df_7d_range.groupby('strike_bucket')['iv'].mean().to_dict()

    chart_matrix = []
    for idx, b_strike in enumerate(target_buckets):
        gex_3d_val = bucket_data_3d['gex'].get(b_strike, 0.0) if b_strike in bucket_data_3d.index else 0.0
        gex_1m_val = bucket_data_1m['gex'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        vanna_val = bucket_data_1m['vanna'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        iv_skew_val = bucket_iv_map.get(b_strike, 0.0)
        
        b_vol = bucket_data_1m['volume'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        b_oi = bucket_data_1m['oi'].get(b_strike, 0.0) if b_strike in bucket_data_1m.index else 0.0
        velocity_pct = (b_vol / b_oi * 100.0) if b_oi > 0 else 0.0
        
        chart_matrix.append({
            "index": idx, "strike": b_strike, 
            "gex_3d": gex_3d_val, "abs_gex_3d": abs(gex_3d_val),
            "gex_1m": gex_1m_val, "abs_gex_1m": abs(gex_1m_val),
            "vanna_exposure": vanna_val, 
            "vanna_flow": vanna_val * iv_shift_multiplier, 
            "velocity_ratio": velocity_pct,
            "iv_skew": iv_skew_val,
            "whale_bullish": whale_matrix[b_strike]["bullish"],
            "whale_bearish": -whale_matrix[b_strike]["bearish"]
        })

    realized_vol_10d_val = calculate_realized_vol_10d(currency)

    pt_gex = (1.5 if net_gex_1m >= 0 else 0.0) + (1.5 if net_gex_3d >= 0 else 0.0)
    pt_flow = 3.0 if net_flow_bias > 0 else 0.0
    pt_price = (1.5 if spot_price > flip_level else 0.0) + (1.5 if spot_price > max_pain_level else 0.0)
    pt_vol = 3.0 if (atm_iv - realized_vol_10d_val) < 0 else 0.0
    total_cohesion_points = pt_gex + pt_flow + pt_price + pt_vol

    hourly_charm_rehedge_contracts = net_charm_accumulator / 24.0

    return {
        "spot": spot_price, 
        "call_gex_1m": call_gex_1m, "put_gex_1m": put_gex_1m, "net_gex_1m": net_gex_1m, "call_weight_1m": call_weight_pct_1m,
        "call_gex_3d": call_gex_3d, "put_gex_3d": put_gex_3d, "net_gex_3d": net_gex_3d, "call_weight_3d": call_weight_pct_3d,
        "max_pain": max_pain_level, "flip": flip_level, "breakout": breakout_price, 
        "resistance": resistance_level, "support": support_level, "call_inflow": total_accumulated_call_flow, 
        "put_inflow": total_accumulated_put_flow, "net_flow": net_flow_bias, "chart_data": chart_matrix,
        "skew_25d": skew_25d_val, "c1_wall": c1_level, "c2_wall": c2_level, "p1_wall": p1_level, "p2_wall": p2_level,
        "implied_vol": atm_iv, "realized_vol": realized_vol_10d_val,
        "trend_score": total_cohesion_points, "pt_gex": pt_gex, "pt_flow": pt_flow, "pt_price": pt_price, "pt_vol": pt_vol,
        "net_charm_flow": hourly_charm_rehedge_contracts,
        "ndf_drift_total": total_cumulative_ndf_drift,
        "aggr_call_ask": call_ask_hit_premium, "aggr_call_bid": call_bid_hit_premium,
        "aggr_put_ask": put_ask_hit_premium, "aggr_put_bid": put_bid_hit_premium,
        "speed_current": net_speed_current, "speed_down_1000": net_speed_down_1000, "speed_up_1000": net_speed_up_1000,
        "iv_direction": "EXPANDING" if iv_shift_multiplier > 0 else "CRUSHING",
        "raw_option_dataframe": bucket_data_1m
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
    abs_axis_3d = ft.ChartAxis(labels=[], labels_size=24)
    net_axis_1m = ft.ChartAxis(labels=[], labels_size=24)
    abs_axis_1m = ft.ChartAxis(labels=[], labels_size=24)
    vanna_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    oi_migration_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    velocity_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    whale_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
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
    
    c1_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400)
    c2_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400)
    p1_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.RED_400)
    p2_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.RED_400)

    skew_25d_txt = ft.Text("0.00% (Neutral)", size=14, weight=ft.FontWeight.BOLD)
    
    pain_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600)
    flip_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    breakout_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_ACCENT)
    res_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_300)
    sup_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.W_600, color=ft.colors.PINK_400)
    
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

    speed_curr_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_down_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_up_txt = ft.Text("0.00", size=14, weight=ft.FontWeight.W_600)
    speed_regime_txt = ft.Text("Stable Neutral", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400)

    flow_1h_txt = ft.Text("0.00 BTC", size=14, weight=ft.FontWeight.BOLD)
    flow_3h_txt = ft.Text("0.00 BTC", size=14, weight=ft.FontWeight.BOLD)
    flow_6h_txt = ft.Text("0.00 BTC", size=14, weight=ft.FontWeight.BOLD)

    cohesion_main_txt = ft.Text("0.0 (Neutral)", size=14, weight=ft.FontWeight.BOLD)
    gex_component_txt = ft.Text("0.0", size=14, color=ft.colors.GREY_400)
    flow_component_txt = ft.Text("0.0", size=14, color=ft.colors.GREY_400)
    price_component_txt = ft.Text("0.0", size=14, color=ft.colors.GREY_400)
    vol_component_txt = ft.Text("0.0", size=14, color=ft.colors.GREY_400)

    charm_flow_metric_txt = ft.Text("0.0 BTC/hr", size=14, weight=ft.FontWeight.BOLD)
    charm_bias_txt = ft.Text("Neutral", size=14, weight=ft.FontWeight.BOLD)

    ndf_drift_metric_txt = ft.Text("$0.0M", size=14, weight=ft.FontWeight.BOLD)
    ndf_structural_signal_txt = ft.Text("Neutral Absorption", size=14, weight=ft.FontWeight.BOLD)

    anomaly_txt_1st = ft.Text("--", size=14, weight=ft.FontWeight.W_600, color=ft.colors.CYAN_200)
    anomaly_txt_2nd = ft.Text("--", size=14, weight=ft.FontWeight.W_600, color=ft.colors.CYAN_200)
    anomaly_txt_3rd = ft.Text("--", size=14, weight=ft.FontWeight.W_600, color=ft.colors.CYAN_200)

    gex_bar_chart_3d = ft.BarChart(bar_groups=[], bottom_axis=net_axis_3d, 
                                   horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), 
                                   vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), 
                                   animate=True, interactive=True, height=240)

    abs_gex_chart_3d = ft.BarChart(
        bar_groups=[], bottom_axis=abs_axis_3d,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    gex_bar_chart_1m = ft.BarChart(
        bar_groups=[], bottom_axis=net_axis_1m,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240)

    abs_gex_chart_1m = ft.BarChart(
        bar_groups=[], bottom_axis=abs_axis_1m,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    vanna_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=vanna_bottom_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    oi_migration_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=oi_migration_bottom_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    velocity_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=velocity_bottom_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    whale_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=whale_bottom_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=260
    )

    id_skew_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=iv_bottom_axis,
        left_axis=iv_left_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
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

            c1_txt.value = f"${m['c1_wall']:,.0f}"
            c2_txt.value = f"${m['c2_wall']:,.0f}"
            p1_txt.value = f"${m['p1_wall']:,.0f}"
            p2_txt.value = f"${m['p2_wall']:,.0f}"
            
            skew_val = m['skew_25d']
            if skew_val <= 0.4 and skew_val >= -0.4:
                skew_25d_txt.value = f"{skew_val:+.2f}% (Neutral)"
                skew_25d_txt.color = ft.colors.GREY_400
            elif skew_val > 0.4:
                skew_25d_txt.value = f"+{skew_val:.2f}% (Bearish)"
                skew_25d_txt.color = ft.colors.RED_400
            else:
                skew_25d_txt.value = f"{skew_val:.2f}% (Bullish)"
                skew_25d_txt.color = ft.colors.GREEN_400
            
            pain_txt.value = f"${m['max_pain']:,.0f}"
            flip_txt.value = f"${m['flip']:,.0f}"
            breakout_txt.value = f"${m['breakout']:,.0f}"
            res_txt.value = f"${m['resistance']:,.0f}"
            sup_txt.value = f"${m['support']:,.0f}"
            
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

            scr = m['trend_score']
            gex_component_txt.value = f"{m['pt_gex']:.1f}"
            flow_component_txt.value = f"{m['pt_flow']:.1f}"
            price_component_txt.value = f"{m['pt_price']:.1f}"
            vol_component_txt.value = f"{m['pt_vol']:.1f}"
            
            if scr <= 3.5:
                cohesion_main_txt.value = f"{scr:.1f} (Strong Bearish)"
                cohesion_main_txt.color = ft.colors.RED_700
            elif scr <= 5.5:
                cohesion_main_txt.value = f"{scr:.1f} (Mild Bearish)"
                cohesion_main_txt.color = ft.colors.RED_400
            elif scr <= 6.5:
                cohesion_main_txt.value = f"{scr:.1f} (Neutral)"
                cohesion_main_txt.color = ft.colors.GREY_400
            elif scr <= 8.5:
                cohesion_main_txt.value = f"{scr:.1f} (Mild Bullish)"
                cohesion_main_txt.color = ft.colors.GREEN_300
            else:
                cohesion_main_txt.value = f"{scr:.1f} (Strong Bullish)"
                cohesion_main_txt.color = ft.colors.GREEN_600

            c_flow_val = m['net_charm_flow']
            charm_flow_metric_txt.value = f"{abs(c_flow_val):,.2f} BTC/hr"
            if c_flow_val < -0.05:
                charm_bias_txt.value = "Automated Buying (Bullish Tailwind)"
                charm_bias_txt.color = ft.colors.GREEN_400
            elif c_flow_val > 0.05:
                charm_bias_txt.value = "Automated Selling (Bearish Headwind)"
                charm_bias_txt.color = ft.colors.RED_400
            else:
                charm_bias_txt.value = "Stable Neutral"
                charm_bias_txt.color = ft.colors.GREY_400

            charm_1h = c_flow_val * 1.0
            charm_3h = c_flow_val * 3.0
            charm_6h = c_flow_val * 6.0

            std_dev_1h = (iv_val / 100.0) * math.sqrt(1.0 / 8760.0)
            std_dev_3h = (iv_val / 100.0) * math.sqrt(3.0 / 8760.0)
            std_dev_6h = (iv_val / 100.0) * math.sqrt(6.0 / 8760.0)

            gex_contracts_3d = m['net_gex_3d'] / m['spot']

            gamma_press_1h = gex_contracts_3d * std_dev_1h
            gamma_press_3h = gex_contracts_3d * std_dev_3h
            gamma_press_6h = gex_contracts_3d * std_dev_6h

            total_1h_flow = charm_1h + gamma_press_1h
            total_3h_flow = charm_3h + gamma_press_3h
            total_6h_flow = charm_6h + gamma_press_6h

            def format_horizon_text(value):
                action = "Expected to BUY" if value >= 0 else "Expected to SELL"
                return f"{action} {abs(value):,.2f} BTC"

            flow_1h_txt.value = format_horizon_text(total_1h_flow)
            flow_1h_txt.color = ft.colors.GREEN_400 if total_1h_flow >= 0 else ft.colors.RED_400

            flow_3h_txt.value = format_horizon_text(total_3h_flow)
            flow_3h_txt.color = ft.colors.GREEN_400 if total_3h_flow >= 0 else ft.colors.RED_400

            flow_6h_txt.value = format_horizon_text(total_6h_flow)
            flow_6h_txt.color = ft.colors.GREEN_400 if total_6h_flow >= 0 else ft.colors.RED_400

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
            
            # --- HOURLY GATED LOGGING GATEWAY SYSTEM ---
            time_now = datetime.now(timezone.utc)
            
            # 1. Logic for open interest migration data to be sent only in the first 4 minutes of every hour
            if time_now.minute <= 4:
                hourly_time_tag = time_now.strftime("%m-%d %H:00")
                
                try:
                    last_oi_element = redis.lindex(REDIS_OI_MIGRATION_KEY, -1)
                    if last_oi_element:
                        try:
                            last_oi_data = json.loads(last_oi_element)
                            if last_oi_data.get("timestamp") == hourly_time_tag:
                                redis.rpop(REDIS_OI_MIGRATION_KEY)
                        except Exception: pass
                        
                    raw_oi_dataframe = m["raw_option_dataframe"]
                    oi_snapshot_map = {}
                    if not raw_oi_dataframe.empty:
                        oi_snapshot_map = raw_oi_dataframe['oi'].to_dict()
                        oi_snapshot_map = {str(k): float(v) for k, v in oi_snapshot_map.items()}
                        
                    oi_history_snapshot = {
                        "timestamp": hourly_time_tag,
                        "oi_distribution": oi_snapshot_map
                    }
                    
                    if redis.llen(REDIS_OI_MIGRATION_KEY) == 0:
                        dummy_snapshot = {
                            "timestamp": (time_now - timedelta(hours=1)).strftime("%m-%d %H:00"),
                            "oi_distribution": {k: float(v) * 0.95 for k, v in oi_snapshot_map.items()}
                        }
                        redis.rpush(REDIS_OI_MIGRATION_KEY, json.dumps(dummy_snapshot))
                        
                    redis.rpush(REDIS_OI_MIGRATION_KEY, json.dumps(oi_history_snapshot))
                    redis.ltrim(REDIS_OI_MIGRATION_KEY, -168, -1)
                    
                except Exception as ex: 
                    print(f"Hourly Gated Database Log Failure: {ex}")

            # --- HISTORICAL OI MIGRATION PARSING ENGINE ---
            historical_oi_deltas = {}
            try:
                oi_snapshots = redis.lrange(REDIS_OI_MIGRATION_KEY, -2, -1)
                if len(oi_snapshots) >= 2:
                    t0_data = json.loads(oi_snapshots[0]).get("oi_distribution", {})
                    t1_data = json.loads(oi_snapshots[1]).get("oi_distribution", {})
                    
                    for k_strike in t1_data.keys():
                        historical_oi_deltas[float(k_strike)] = float(t1_data[k_strike]) - float(t0_data.get(k_strike, 0.0))
            except Exception as ex:
                print(f"Migration Parsing Fail: {ex}")
            
            groups_net_3d, groups_abs_3d, groups_net_1m, groups_abs_1m, groups_vanna, groups_oi_migration, groups_velocity, groups_whale, iv_bar_groups, new_labels, min_dist, spot_index = [], [], [], [], [], [], [], [], [], [], float('inf'), -1
            
            max_abs_vanna_exposure = 0.0001
            max_abs_oi_delta = 0.0001

            for item in m['chart_data']:
                dist = abs(item['strike'] - m['spot'])
                if dist < min_dist: min_dist, spot_index = dist, item['index']
                
                if abs(item['vanna_exposure']) > max_abs_vanna_exposure:
                    max_abs_vanna_exposure = abs(item['vanna_exposure'])
                    
                stk = item['strike']
                oi_change = historical_oi_deltas.get(stk, 0.0)
                if abs(oi_change) > max_abs_oi_delta:
                    max_abs_oi_delta = abs(oi_change)
            
            vanna_exposure_bound = max_abs_vanna_exposure * 1.15
            vanna_bar_chart.min_y = -vanna_exposure_bound
            vanna_bar_chart.max_y = vanna_exposure_bound

            oi_migration_bound = max_abs_oi_delta * 1.15
            oi_migration_bar_chart.min_y = -oi_migration_bound
            oi_migration_bar_chart.max_y = oi_migration_bound
            
            valid_ivs = [item['iv_skew'] for item in m['chart_data'] if item['iv_skew'] > 0]
            max_iv = max_iv if (max_iv := max(valid_ivs, default=100.0)) > 0 else 100.0
            min_iv = min_iv if (min_iv := min(valid_ivs, default=0.0)) > 0 else 0.0
            
            floor_y = math.floor(min_iv / 10.0) * 10.0
            ceil_y = math.ceil(max_iv / 10.0) * 10.0
            if ceil_y == floor_y: ceil_y += 10.0
            
            id_skew_bar_chart.min_y = floor_y
            id_skew_bar_chart.max_y = ceil_y

            y_iv_labels = []
            curr_y = floor_y
            while curr_y <= ceil_y:
                y_iv_labels.append(ft.ChartAxisLabel(value=curr_y, label=ft.Text(f"{int(curr_y)}%", size=10, color=ft.colors.GREY_400)))
                curr_y += 10.0
            iv_left_axis.labels = y_iv_labels
            
            sorted_velocity_items = sorted(m['chart_data'], key=lambda x: x['velocity_ratio'], reverse=True)
            top_anomalies = [item for item in sorted_velocity_items if item['velocity_ratio'] > 0][:3]
            
            anomaly_txt_1st.value = "No dynamic target detected"
            anomaly_txt_2nd.value = "--"
            anomaly_txt_3rd.value = "--"
            
            if len(top_anomalies) >= 1:
                anomaly_txt_1st.value = f"${top_anomalies[0]['strike']/1000:.0f}k Strike ({top_anomalies[0]['velocity_ratio']:.1f}%)"
            if len(top_anomalies) >= 2:
                anomaly_txt_2nd.value = f"${top_anomalies[1]['strike']/1000:.0f}k Strike ({top_anomalies[1]['velocity_ratio']:.1f}%)"
            if len(top_anomalies) >= 3:
                anomaly_txt_3rd.value = f"${top_anomalies[2]['strike']/1000:.0f}k Strike ({top_anomalies[2]['velocity_ratio']:.1f}%)"

            for item in m['chart_data']:
                strike_val, is_spot = item['strike'], (item['index'] == spot_index)
                val_3d, abs_3d, val_1m, abs_1m, v_exposure, vel_ratio, iv_val_item = item['gex_3d'], item['abs_gex_3d'], item['gex_1m'], item['abs_gex_1m'], item['vanna_exposure'], item['velocity_ratio'], item['iv_skew']
                w_bull, w_bear = item['whale_bullish'], item['whale_bearish']

                groups_net_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_3d, color=ft.colors.GREEN_400 if val_3d >= 0 else ft.colors.RED_400, width=12, border_radius=2)]))
                groups_abs_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_3d, color=ft.colors.YELLOW, width=12, border_radius=2)]))
                groups_net_1m.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_1m, color="#bab7ab" if val_1m >= 0 else "#1661b4", width=12, border_radius=2)]))
                groups_abs_1m.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_1m, color="#ab47bc", width=12, border_radius=2)]))
                
                groups_vanna.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=v_exposure, color="#d26e5a" if v_exposure >= 0 else ft.colors.WHITE70, width=12, border_radius=2)]))
                
                oi_delta = historical_oi_deltas.get(strike_val, 0.0)
                groups_oi_migration.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=oi_delta, color="#35c2b3" if oi_delta >= 0 else "#7948be", width=12, border_radius=2)]))

                groups_velocity.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=vel_ratio, color="#0097a7", width=12, border_radius=2)]))

                groups_whale.append(ft.BarChartGroup(
                    x=item['index'],
                    bar_rods=[
                        ft.BarChartRod(from_y=0, to_y=w_bull, color=ft.colors.GREEN_400, width=10, border_radius=1),
                        ft.BarChartRod(from_y=0, to_y=w_bear, color=ft.colors.RED_400, width=10, border_radius=1)
                    ]
                ))

                iv_bar_groups.append(ft.BarChartGroup(
                    x=item['index'],
                    bar_rods=[ft.BarChartRod(from_y=floor_y, to_y=iv_val_item if iv_val_item > 0 else floor_y, color=ft.colors.ORANGE_700, width=12, border_radius=2)]
                ))
                
                if strike_val % 2000 == 0:
                    label_color = ft.colors.BLUE_200 if is_spot else ft.colors.GREY_400
                    new_labels.append(ft.ChartAxisLabel(value=item['index'], label=ft.Text(f"{strike_val/1000:.0f}k", size=10, color=label_color, rotate=45, weight=ft.FontWeight.BOLD if is_spot else ft.FontWeight.NORMAL)))
            
            gex_bar_chart_3d.bar_groups = groups_net_3d
            net_axis_3d.labels = new_labels
            abs_gex_chart_3d.bar_groups = groups_abs_3d
            abs_axis_3d.labels = new_labels

            gex_bar_chart_1m.bar_groups = groups_net_1m
            net_axis_1m.labels = list(new_labels)
            abs_gex_chart_1m.bar_groups = groups_abs_1m
            abs_axis_1m.labels = list(new_labels)
            
            vanna_bar_chart.bar_groups = groups_vanna
            vanna_bottom_axis.labels = list(new_labels)

            oi_migration_bar_chart.bar_groups = groups_oi_migration
            oi_migration_bottom_axis.labels = list(new_labels)

            velocity_bar_chart.bar_groups = groups_velocity
            velocity_bottom_axis.labels = list(new_labels)

            whale_bar_chart.bar_groups = groups_whale
            whale_bottom_axis.labels = list(new_labels)

            id_skew_bar_chart.bar_groups = iv_bar_groups
            iv_bottom_axis.labels = list(new_labels)
            
            page.update()

    page.add(
        ft.Row([ft.Text("DERIBIT GEX DASHBOARD", size=20, weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("Refresh", on_click=refresh_dashboard, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)))], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("Bitcoin Spot Price", size=11, color=ft.colors.GREY_500), spot_price_container], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
        create_section_header("NET GAMMA EXPOSURE BY STRIKE (3D)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart_3d)),
        
        create_section_header("ABS GAMMA EXPOSURE BY STRIKE (3D)"),
        ft.Card(content=ft.Container(padding=15, content=ft.Column([
            abs_gex_chart_3d,
            ft.Container(height=10),
            ui_row_item("Call Concetration (C1)", c1_txt),
            ui_row_item("Call Concetration (C2)", c2_txt),
            ui_row_item("Put Concetration (P1)", p1_txt),
            ui_row_item("Put Concetration (P2)", p2_txt)
        ]))),

        create_section_header("IMPORTANT LEVELS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Max Pain", pain_txt), ui_row_item("Flip Zone", flip_txt), ui_row_item("Breakout Price", breakout_txt), ui_row_item("Resistance Level", res_txt), ui_row_item("Support Level", sup_txt)]))),

        create_section_header("NET GAMMA EXPOSURE BY STRIKE (1M)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart_1m)),

        create_section_header("ABS GAMMA EXPOSURE BY STRIKE (1M)"),
        ft.Card(content=ft.Container(padding=15, content=abs_gex_chart_1m)),
        
        create_section_header("INTRADAY GAMMA VELOCITY PROFILE (VOLUME / OI)"),
        ft.Card(content=ft.Container(padding=15, content=ft.Column([
            velocity_bar_chart,
            ft.Container(height=10),
            ui_row_item("1st Anomaly", anomaly_txt_1st),
            ui_row_item("2nd Anomaly", anomaly_txt_2nd),
            ui_row_item("3rd Anomaly", anomaly_txt_3rd)
        ]))),

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
            ui_row_item("Current 25D strike Skew", skew_25d_txt)
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

        create_section_header("INSTITUTIONAL COHESION (ITC SCORE)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("GEX Regime Component", gex_component_txt),
            ui_row_item("Options Tape Flow Component", flow_component_txt),
            ui_row_item("Price Structure Component", price_component_txt),
            ui_row_item("Volatility Setup Component", vol_component_txt),
            ui_row_item("ITC Score (12)", cohesion_main_txt)
        ]))),

        create_section_header("CHARM EXPOSURE ANALYSIS (CEX)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Estimated Decay Rehedge Flow", charm_flow_metric_txt),
            ui_row_item("Dealer Bias", charm_bias_txt)
        ]))),

        create_section_header("DEALER REAL-TIME HEDGING FLOW ESTIMATOR"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Next 1H", flow_1h_txt),
            ui_row_item("Next 3H", flow_3h_txt),
            ui_row_item("Next 6H", flow_6h_txt)
        ]))),

        create_section_header("CUMULATIVE DELTA PREMIUM DRIFT (NDF)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("24H Net Delta Flow Drift", ndf_drift_metric_txt),
            ui_row_item("Tape", ndf_structural_signal_txt)
        ]))),

        create_section_header("LARGE LOT BLOCKS DETECTOR"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=whale_bar_chart))
    )
    refresh_dashboard()

if __name__ == "__main__":
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=None)
