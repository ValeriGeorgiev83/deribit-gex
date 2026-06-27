import os
import math
import json
import requests
import pandas as pd
import numpy as np
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
REDIS_WHALE_KEY = "deribit_whale_blocks_24h"
MAX_HISTORY_POINTS = 3500
WHALE_THRESHOLD_USD = 250000.0

def calculate_realized_vol_10d(currency="BTC"):
    """Fetches the last 10 daily close prices to calculate annualized close-to-close realized volatility."""
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        start_ts = now_ts - (12 * 86400) # Fetch slightly more than 10 days to guarantee intervals
        
        url = f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name={currency.upper()}-USD&resolution=1D&start_timestamp={start_ts * 1000}&end_timestamp={now_ts * 1000}"
        res = requests.get(url).json()
        closes = res.get('result', {}).get('c', [])
        
        if len(closes) < 10:
            return 50.0 # Standard structural fall-back baseline percentage
            
        # Keep exactly the trailing 10 daily close data points
        target_closes = closes[-10:]
        log_returns = np.diff(np.log(target_closes))
        
        # Annualizing daily close-to-close standard deviation (sqrt of 365 trading days in crypto)
        daily_std = np.std(log_returns, ddof=1)
        annualized_rv = daily_std * math.sqrt(365) * 100.0
        return float(annualized_rv)
    except Exception:
        return 50.0

def fetch_deribit_gex(currency="BTC"):
    """Fetches and calculates GEX, market flow, whale blocks, IV/RV metrics from Deribit."""
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
        
        # Capture At-The-Money (ATM) Implied Volatility on near-dated expiries
        if days_to_expiry <= 7.0:
            dist = abs(spot_price - strike)
            if dist < min_strike_dist:
                min_strike_dist = dist
                atm_iv = iv * 100.0
            
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
            'iv': iv * 100.0, 
            'days_to_expiry': days_to_expiry
        })
        
    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None
    
    df_1m = base_df[base_df['days_to_expiry'] <= 30.0]
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = df_1m

    # --- 1M CALCULATION ENGINE ---
    call_df_1m = df_1m[df_1m['type'] == 'C']
    put_df_1m = df_1m[df_1m['type'] == 'P']
    call_gex_1m = call_df_1m['gex'].sum()
    put_gex_1m = put_df_1m['gex'].sum()
    net_gex_1m = call_gex_1m + put_gex_1m
    total_abs_gex_1m = abs(call_gex_1m) + abs(put_gex_1m)
    call_weight_pct_1m = (abs(call_gex_1m) / total_abs_gex_1m * 100) if total_abs_gex_1m > 0 else 50.0
    
    # --- 3D CALCULATION ENGINE ---
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
    
    # --- 7D EXPIRATION ENGINE FOR IV SKEW ---
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
    detected_whale_blocks = []
    
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

            if ins_name.endswith('-C'):
                if direction == 'buy': net_call_fiat_flow += fiat_notional_value
                else: net_call_fiat_flow -= fiat_notional_value
            elif ins_name.endswith('-P'):
                if direction == 'buy': net_put_fiat_flow -= fiat_notional_value
                else: net_put_fiat_flow += fiat_notional_value
                
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
            flow_snapshot = {"timestamp": current_ts, "call_flow": round(net_call_fiat_flow, 2), "put_flow": round(net_put_fiat_flow, 2)}
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
    net_flow_bias = total_accumulated_call_flow + (total_accumulated_put_flow * -1)

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

    df_chart_range_1m = df_1m[(df_1m['strike'] >= lower_bound) & (df_1m['strike'] <= upper_bound)].copy()
    df_chart_range_1m['strike_bucket'] = df_chart_range_1m['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    bucket_data_1m = df_chart_range_1m.groupby('strike_bucket').agg({'gex': 'sum'})
    
    df_7d_range = df_7d[(df_7d['strike'] >= lower_bound) & (df_7d['strike'] <= upper_bound)].copy() if not df_7d.empty else pd.DataFrame()
    bucket_iv_map = {}
    if not df_7d_range.empty:
        df_7d_range['strike_bucket'] = df_7d_range['strike'].apply(lambda x: round(x / 1000.0) * 1000)
        bucket_iv_map = df_7d_range.groupby('strike_bucket')['iv'].mean().to_dict()

    chart_matrix = []
    for idx, b_strike in enumerate(target_buckets):
        gex_3d_val = bucket_data_3d.get('gex', {}).get(b_strike, 0.0)
        gex_1m_val = bucket_data_1m.get('gex', {}).get(b_strike, 0.0)
        iv_skew_val = bucket_iv_map.get(b_strike, 0.0)
        
        chart_matrix.append({
            "index": idx, "strike": b_strike, 
            "gex_3d": gex_3d_val, "abs_gex_3d": abs(gex_3d_val),
            "gex_1m": gex_1m_val, "abs_gex_1m": abs(gex_1m_val),
            "iv_skew": iv_skew_val,
            "whale_bullish": whale_matrix[b_strike]["bullish"],
            "whale_bearish": -whale_matrix[b_strike]["bearish"]
        })

    # --- EXECUTETraili trailing 10D RV EXTRACTION PIPELINE ---
    realized_vol_10d_val = calculate_realized_vol_10d(currency)

    return {
        "spot": spot_price, 
        "call_gex_1m": call_gex_1m, "put_gex_1m": put_gex_1m, "net_gex_1m": net_gex_1m, "call_weight_1m": call_weight_pct_1m,
        "call_gex_3d": call_gex_3d, "put_gex_3d": put_gex_3d, "net_gex_3d": net_gex_3d, "call_weight_3d": call_weight_pct_3d,
        "max_pain": max_pain_level, "flip": flip_level, "breakout": breakout_price, 
        "resistance": resistance_level, "support": support_level, "call_inflow": total_accumulated_call_flow, 
        "put_inflow": total_accumulated_put_flow, "net_flow": net_flow_bias, "chart_data": chart_matrix,
        "skew_25d": skew_25d_val, "c1_wall": c1_level, "c2_wall": c2_level, "p1_wall": p1_level, "p2_wall": p2_level,
        "implied_vol": atm_iv, "realized_vol": realized_vol_10d_val
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
    whale_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_left_axis = ft.ChartAxis(labels=[], labels_size=42)

    spot_txt = ft.Text("$0.00", size=22, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_400)
    
    call_gex_txt_1m = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    put_gex_txt_1m = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    net_gex_txt_1m = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt_1m = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_300)

    call_gex_txt_3d = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    put_gex_txt_3d = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    net_gex_txt_3d = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt_3d = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_800)
    
    c1_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400)
    c2_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400)
    p1_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.RED_400)
    p2_txt = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.RED_400)

    skew_25d_txt = ft.Text("0.00% (Neutral)", size=14, weight=ft.FontWeight.BOLD)
    pain_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600)
    flip_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    breakout_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_ACCENT)
    res_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_300)
    sup_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PINK_400)
    
    inflows_call_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)
    outflows_put_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)
    net_flow_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)

    # --- NEW: VOLATILITY ANALYTICS INTERFACE ELEMENTS ---
    iv_metric_txt = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600)
    rv_metric_txt = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600)
    vol_variance_txt = ft.Text("0.0% (Neutral)", size=18, weight=ft.FontWeight.BOLD)

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
        animate=True, interactive=True, height=240
    )

    abs_gex_chart_1m = ft.BarChart(
        bar_groups=[], bottom_axis=abs_axis_1m,
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

    iv_skew_bar_chart = ft.BarChart(
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
            spot_txt.value = f"${m['spot']:,.2f}"
            
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

            # --- FIXED: RE-BIND METRICS FOR DYNAMIC VOLATILITY INTERFACE CARD ---
            iv_val, rv_val = m['implied_vol'], m['realized_vol']
            iv_metric_txt.value = f"{iv_val:.1f}%"
            rv_metric_txt.value = f"{rv_val:.1f}%"
            
            variance_spread = iv_val - rv_val
            # FIXED: If IV < RV (Negative Variance Spread) Option premiums are deep discount -> Breakout (Green)
            if variance_spread < 0:
                vol_variance_txt.value = f"{variance_spread:+.1f}% (Breakout Coming)"
                vol_variance_txt.color = ft.colors.GREEN_400
            else:
                vol_variance_txt.value = f"{variance_spread:+.1f}% (Sideways Risk)"
                vol_variance_txt.color = ft.colors.RED_400
            
            time_now = datetime.now(timezone.utc)
            current_refresh_ts = time_now.strftime("%m-%d %H:%M")
            try:
                last_gex_element = redis.lindex(REDIS_KEY, -1)
                is_gex_dup = False
                if last_gex_element:
                    try:
                        logged_data = json.loads(last_gex_element)
                        logged_ts = logged_data.get("timestamp") or datetime.fromtimestamp(logged_data.get("epoch"), tz=timezone.utc).strftime("%m-%d %H:%M")
                        if logged_ts == current_refresh_ts: is_gex_dup = True
                    except Exception: pass
                if not is_gex_dup:
                    snapshot = {"timestamp": current_refresh_ts, "gex": round(m['net_gex_1m'], 2)}
                    redis.rpush(REDIS_KEY, json.dumps(snapshot))
                    redis.ltrim(REDIS_KEY, -MAX_HISTORY_POINTS, -1)
            except Exception as ex: print(f"Cloud Logging Interrupted: {ex}")
            
            groups_net_3d, groups_abs_3d, groups_net_1m, groups_abs_1m, groups_whale, iv_bar_groups, new_labels, min_dist, spot_index = [], [], [], [], [], [], [], float('inf'), -1
            for item in m['chart_data']:
                dist = abs(item['strike'] - m['spot'])
                if dist < min_dist: min_dist, spot_index = dist, item['index']
            
            valid_ivs = [item['iv_skew'] for item in m['chart_data'] if item['iv_skew'] > 0]
            max_iv = max_iv if (max_iv := max(valid_ivs, default=100.0)) > 0 else 100.0
            min_iv = min_iv if (min_iv := min(valid_ivs, default=0.0)) > 0 else 0.0
            
            floor_y = math.floor(min_iv / 10.0) * 10.0
            ceil_y = math.ceil(max_iv / 10.0) * 10.0
            if ceil_y == floor_y: ceil_y += 10.0
            
            iv_skew_bar_chart.min_y = floor_y
            iv_skew_bar_chart.max_y = ceil_y

            y_iv_labels = []
            curr_y = floor_y
            while curr_y <= ceil_y:
                y_iv_labels.append(ft.ChartAxisLabel(value=curr_y, label=ft.Text(f"{int(curr_y)}%", size=10, color=ft.colors.GREY_400)))
                curr_y += 10.0
            iv_left_axis.labels = y_iv_labels
            
            for item in m['chart_data']:
                strike_val, is_spot = item['strike'], (item['index'] == spot_index)
                val_3d, abs_3d, val_1m, abs_1m, iv_val_item = item['gex_3d'], item['abs_gex_3d'], item['gex_1m'], item['abs_gex_1m'], item['iv_skew']
                w_bull, w_bear = item['whale_bullish'], item['whale_bearish']

                groups_net_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_3d, color=ft.colors.GREEN_400 if val_3d >= 0 else ft.colors.RED_400, width=12, border_radius=2)]))
                groups_abs_3d.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_3d, color=ft.colors.YELLOW, width=12, border_radius=2)]))
                groups_net_1m.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val_1m, color="#bab7ab" if val_1m >= 0 else "#1661b4", width=12, border_radius=2)]))
                groups_abs_1m.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_1m, color="#ab47bc", width=12, border_radius=2)]))
                
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
            
            whale_bar_chart.bar_groups = groups_whale
            whale_bottom_axis.labels = list(new_labels)

            iv_skew_bar_chart.bar_groups = iv_bar_groups
            iv_bottom_axis.labels = list(new_labels)
            
            page.update()

    page.add(
        ft.Row([ft.Text("DERIBIT GEX DASHBOARD", size=20, weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("Refresh", on_click=refresh_dashboard, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)))], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("BTC UNDERLYING SPOT", size=11, color=ft.colors.GREY_500), spot_txt], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
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

        create_section_header("NET GAMMA EXPOSURE BY STRIKE (1M)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart_1m)),

        create_section_header("ABS GAMMA EXPOSURE BY STRIKE (1M)"),
        ft.Card(content=ft.Container(padding=15, content=abs_gex_chart_1m)),
        
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
            iv_skew_bar_chart,
            ft.Container(height=10),
            ui_row_item("Current 25D strike Skew", skew_25d_txt)
        ]))),
        
        create_section_header("IMPORTANT LEVELS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Max Pain", pain_txt), ui_row_item("Flip Zone", flip_txt), ui_row_item("Breakout Price", breakout_txt), ui_row_item("Resistance Level", res_txt), ui_row_item("Support Level", sup_txt)]))),
        create_section_header("24H ACCUMULATED ORDER FLOW ANALYSIS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("NET CALL INFLOWS", inflows_call_txt), ui_row_item("NET PUT INFLOWS", outflows_put_txt), ui_row_item("NET PREMIUM BIAS", net_flow_txt)]))),

        # --- FIXED: VOLATILITY VARIANCE ANALYSIS CARD INSERTED CLEANLY BELOW ORDER FLOW CARD ---
        create_section_header("VOLATILITY VARIANCE ANALYSIS (10D)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Implied Volatility (IV)", iv_metric_txt),
            ui_row_item("Realized Volatility (RV)", rv_metric_txt),
            ui_row_item("IV - RV Variation", vol_variance_txt)
        ]))),

        create_section_header("LARGE LOT BLOCKS DETECTOR"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=whale_bar_chart))
    )
    refresh_dashboard()

if __name__ == "__main__":
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=None)
