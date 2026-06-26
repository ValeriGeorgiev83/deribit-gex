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
    """Fetches and calculates GEX, market flow, and IV Skew metrics from Deribit."""
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
            'iv': iv * 100.0, 
            'days_to_expiry': days_to_expiry
        })
        
    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None
    
    df_3m = base_df[base_df['days_to_expiry'] <= 90.0]
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = df_3m

    # --- 3M CALCULATION ENGINE ---
    call_df_3m = df_3m[df_3m['type'] == 'C']
    put_df_3m = df_3m[df_3m['type'] == 'P']
    call_gex_3m = call_df_3m['gex'].sum()
    put_gex_3m = put_df_3m['gex'].sum()
    net_gex_3m = call_gex_3m + put_gex_3m
    total_abs_gex_3m = abs(call_gex_3m) + abs(put_gex_3m)
    call_weight_pct_3m = (abs(call_gex_3m) / total_abs_gex_3m * 100) if total_abs_gex_3m > 0 else 50.0
    
    # --- 3D CALCULATION ENGINE ---
    call_df_3d = df_3d[df_3d['type'] == 'C']
    put_df_3d = df_3d[df_3d['type'] == 'P']
    call_gex_3d = call_df_3d['gex'].sum()
    put_gex_3d = put_df_3d['gex'].sum()
    net_gex_3d = call_gex_3d + put_gex_3d
    total_abs_gex_3d = abs(call_gex_3d) + abs(put_gex_3d)
    call_weight_pct_3d = (abs(call_gex_3d) / total_abs_gex_3d * 100) if total_abs_gex_3d > 0 else 50.0
    
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

    # --- TIME-BASED HISTORICAL LOG CLEAN-UP ENGINE (LOGS 3D EXPRIY NET GEX) ---
    try:
        last_gex_element = redis.lindex(REDIS_KEY, -1)
        is_gex_dup = False
        if last_gex_element:
            try:
                logged_data = json.loads(last_gex_element)
                logged_ts = logged_data.get("timestamp") or datetime.fromtimestamp(logged_data.get("epoch"), tz=timezone.utc).strftime("%m-%d %H:%M")
                if logged_ts == current_ts: is_gex_dup = True
            except Exception: pass

        if not is_gex_dup:
            snapshot = {
                "timestamp": current_ts,
                "gex": round(net_gex_3d, 2)  # Log options with expiry not later than 3 days
            }
            redis.rpush(REDIS_KEY, json.dumps(snapshot))
            redis.ltrim(REDIS_KEY, -MAX_HISTORY_POINTS, -1)
    except Exception as ex:
        print(f"Cloud Logging Interrupted: {ex}")

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
            else: records_to_remove_count += 1
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
    center_spot_1k = round(spot_price / 1000.0) * 1000
    lower_bound = center_spot_1k - 8000
    upper_bound = center_spot_1k + 8000
    
    df_chart_range = df_3d[(df_3d['strike'] >= lower_bound) & (df_3d['strike'] <= upper_bound)].copy()
    if df_chart_range.empty:
        df_chart_range = df_3m[(df_3m['strike'] >= lower_bound) & (df_3m['strike'] <= upper_bound)].copy()
        
    df_chart_range['strike_bucket'] = df_chart_range['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    df_chart_range['abs_gex_contribution'] = df_chart_range['gex'].abs()
    
    df_chart_range['bullish_gex'] = df_chart_range.apply(
        lambda r: abs(r['gex']) if (r['type'] == 'C' and r['gex'] < 0) or (r['type'] == 'P' and r['gex'] > 0) else 0.0, axis=1
    )
    df_chart_range['bearish_gex'] = df_chart_range.apply(
        lambda r: -abs(r['gex']) if (r['type'] == 'C' and r['gex'] > 0) or (r['type'] == 'P' and r['gex'] < 0) else 0.0, axis=1
    )
    
    bucket_data = df_chart_range.groupby('strike_bucket').agg({
        'gex': 'sum', 'abs_gex_contribution': 'sum', 'bullish_gex': 'sum', 'bearish_gex': 'sum'
    })
    
    df_7d_range = df_7d[(df_7d['strike'] >= lower_bound) & (df_7d['strike'] <= upper_bound)].copy() if not df_7d.empty else pd.DataFrame()
    bucket_iv_map = {}
    if not df_7d_range.empty:
        df_7d_range['strike_bucket'] = df_7d_range['strike'].apply(lambda x: round(x / 1000.0) * 1000)
        bucket_iv_map = df_7d_range.groupby('strike_bucket')['iv'].mean().to_dict()

    target_buckets = list(range(int(lower_bound), int(upper_bound) + 1000, 1000))
    chart_matrix = []
    
    for idx, b_strike in enumerate(target_buckets):
        gex_val = bucket_data.get('gex', {}).get(b_strike, 0.0)
        abs_gex_val = bucket_data.get('abs_gex_contribution', {}).get(b_strike, 0.0)
        bull_val = bucket_data.get('bullish_gex', {}).get(b_strike, 0.0)
        bear_val = bucket_data.get('bearish_gex', {}).get(b_strike, 0.0)
        iv_skew_val = bucket_iv_map.get(b_strike, 0.0)
        
        chart_matrix.append({
            "index": idx, "strike": b_strike, "gex": gex_val, "abs_gex": abs_gex_val,
            "bullish_gex": bull_val, "bearish_gex": bear_val, "iv_skew": iv_skew_val
        })

    return {
        "spot": spot_price, 
        "call_gex_3m": call_gex_3m, "put_gex_3m": put_gex_3m, "net_gex_3m": net_gex_3m, "call_weight_3m": call_weight_pct_3m,
        "call_gex_3d": call_gex_3d, "put_gex_3d": put_gex_3d, "net_gex_3d": net_gex_3d, "call_weight_3d": call_weight_pct_3d,
        "max_pain": max_pain_level, "flip": flip_level, "breakout": breakout_price, 
        "resistance": resistance_level, "support": support_level, "call_inflow": total_accumulated_call_flow, 
        "put_inflow": total_accumulated_put_flow, "net_flow": net_flow_bias, "chart_data": chart_matrix,
        "skew_25d": skew_25d_val
    }

def fmt_gex(val):
    sign = "+" if val >= 0 else "-"
    abs_val = abs(val)
    return f"{sign}{abs_val/1000:.1f}k" if abs_val >= 1000 else f"{sign}{abs_val:.1f}"

def fmt_unsigned_fiat_flow(val):
    millions_val = abs(val) / 1000000.0
    return f"{millions_val:,.1f}M"

def main(page: ft.Page):
    page.title = "Deribit GEX Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14

    net_axis = ft.ChartAxis(labels=[], labels_size=24)
    abs_axis = ft.ChartAxis(labels=[], labels_size=24)
    breakdown_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    iv_left_axis = ft.ChartAxis(labels=[], labels_size=42)
    
    # --- NEW HISTORICAL NET CHANGE CHART AXES ---
    history_change_bottom_axis = ft.ChartAxis(labels=[], labels_size=24)
    history_change_left_axis = ft.ChartAxis(labels=[], labels_size=42)

    spot_txt = ft.Text("$0.00", size=22, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_400)
    
    call_gex_txt_3m = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_400)
    put_gex_txt_3m = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.RED_400)
    net_gex_txt_3m = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt_3m = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_300)

    call_gex_txt_3d = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    put_gex_txt_3d = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.INDIGO_400)
    net_gex_txt_3d = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt_3d = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_800)
    
    skew_25d_txt = ft.Text("0.00%", size=18, weight=ft.FontWeight.BOLD)
    
    pain_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600)
    flip_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    breakout_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_ACCENT)
    res_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_300)
    sup_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PINK_400)
    
    inflows_call_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)
    outflows_put_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)
    net_flow_txt = ft.Text("0.0M", size=18, weight=ft.FontWeight.W_600)

    gex_bar_chart = ft.BarChart(bar_groups=[], bottom_axis=net_axis, 
                                horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), 
                                vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), 
                                animate=True, interactive=True, height=240)

    abs_gex_chart = ft.BarChart(
        bar_groups=[], bottom_axis=abs_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    breakdown_gex_chart = ft.BarChart(
        bar_groups=[], bottom_axis=breakdown_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    iv_skew_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=iv_bottom_axis,
        left_axis=iv_left_axis,
        horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5),
        animate=True, interactive=True, height=240
    )

    # --- NEW REQUESTED NET GAMMA CHANGE (24H) BAR CANVAS ---
    history_change_bar_chart = ft.BarChart(
        bar_groups=[], bottom_axis=history_change_bottom_axis,
        left_axis=history_change_left_axis,
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
            
            call_gex_txt_3m.value = fmt_gex(m['call_gex_3m'])
            put_gex_txt_3m.value = fmt_gex(m['put_gex_3m'])
            net_gex_txt_3m.value = fmt_gex(m['net_gex_3m'])
            net_gex_txt_3m.color = ft.colors.GREEN_400 if m['net_gex_3m'] >= 0 else ft.colors.RED_400
            weight_txt_3m.value = f"{m['call_weight_3m']:.1f}%"

            call_gex_txt_3d.value = fmt_gex(m['call_gex_3d'])
            put_gex_txt_3d.value = fmt_gex(m['put_gex_3d'])
            net_gex_txt_3d.value = fmt_gex(m['net_gex_3d'])
            net_gex_txt_3d.color = ft.colors.ORANGE_400 if m['net_gex_3d'] >= 0 else ft.colors.INDIGO_400
            weight_txt_3d.value = f"{m['call_weight_3d']:.1f}%"
            
            skew_25d_txt.value = f"{m['skew_25d']:+.2f}%"
            if m['skew_25d'] > 0: skew_25d_txt.color = ft.colors.GREEN_400
            elif m['skew_25d'] < 0: skew_25d_txt.color = ft.colors.RED_400
            else: skew_25d_txt.color = ft.colors.GREY_400
            
            pain_txt.value = f"${m['max_pain']:,.0f}"
            flip_txt.value = f"${m['flip']:,.0f}"
            breakout_txt.value = f"${m['breakout']:,.0f}"
            res_txt.value = f"${m['resistance']:,.0f}"
            sup_txt.value = f"${m['support']:,.0f}"
            
            inflows_call_txt.value = fmt_unsigned_fiat_flow(m['call_inflow'])
            inflows_call_txt.color = ft.colors.GREEN_400 if m['call_inflow'] >= 0 else ft.colors.RED_400
            outflows_put_txt.value = fmt_unsigned_fiat_flow(m['put_inflow'])
            outflows_put_txt.color = ft.colors.GREEN_400 if m['put_inflow'] >= 0 else ft.colors.RED_400
            net_flow_txt.value = fmt_unsigned_fiat_flow(m['net_flow'])
            net_flow_txt.color = ft.colors.GREEN_400 if m['net_flow'] >= 0 else ft.colors.RED_400
            
            # --- POPULATE ROLLING HISTORICAL TREND (AVERAGED HOURLY BLOCKS) ---
            time_now = datetime.now(timezone.utc)
            try:
                raw_records = redis.lrange(REDIS_KEY, 0, -1)
                if raw_records:
                    history_points_list = []
                    for record in raw_records:
                        try:
                            data = json.loads(record)
                            if "timestamp" in data:
                                ts_str = data['timestamp']
                            elif "epoch" in data:
                                ts_str = datetime.fromtimestamp(data['epoch'], tz=timezone.utc).strftime("%m-%d %H:%M")
                            else: continue
                            
                            if "-" in ts_str:
                                rec_time = datetime.strptime(f"{time_now.year}-{ts_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                            else:
                                rec_time = datetime.strptime(f"{time_now.year}-{time_now.month}-{ts_str}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                            
                            if rec_time > time_now: rec_time = rec_time.replace(year=time_now.year - 1)
                            
                            hours_diff = (time_now - rec_time).total_seconds() / 3600.0
                            if hours_diff <= 24.0:
                                data['hours_ago'] = hours_diff
                                data['round_hour_str'] = rec_time.strftime("%H")
                                history_points_list.append(data)
                        except Exception: continue

                    if history_points_list:
                        df_hist = pd.DataFrame(history_points_list)
                        
                        # Generate 24 structured sequential baseline hour blocks ending at the current hour
                        sequence_hours_list = []
                        for h in range(23, -1, -1):
                            sequence_hours_list.append((time_now - timedelta(hours=h)).strftime("%H"))

                        # Compute the exact mean net gamma for each hour block using Pandas aggregate maps
                        grouped_hist = df_hist.groupby('round_hour_str')['gex'].mean().to_dict()
                        
                        hist_bars = []
                        hist_labels = []
                        
                        # Fix vertical axis parameters to 50M intervals dynamically
                        all_means_in_millions = [grouped_hist.get(h, 0.0) / 1000000.0 for h in sequence_hours_list]
                        largest_hist_abs = max(max([abs(m) for m in all_means_in_millions], default=50.0), 50.0)
                        fixed_hist_bound = math.ceil(largest_hist_abs / 50.0) * 50.0
                        
                        history_change_bar_chart.min_y = -fixed_hist_bound * 1000000.0
                        history_change_bar_chart.max_y = fixed_hist_bound * 1000000.0
                        
                        y_hist_labels = []
                        curr_h_step = -fixed_bound_step = fixed_hist_bound
                        while curr_h_step <= fixed_hist_bound:
                            h_sign = "+" if curr_h_step > 0 else ""
                            lbl_text = f"{h_sign}{int(curr_h_step)}M" if curr_h_step != 0 else "0"
                            y_hist_labels.append(ft.ChartAxisLabel(value=curr_h_step * 1000000.0, label=ft.Text(lbl_text, size=10, color=ft.colors.GREY_400)))
                            curr_h_step += 50.0
                        history_change_left_axis.labels = y_hist_labels

                        for idx, h_str in enumerate(sequence_hours_list):
                            avg_gex_val = grouped_hist.get(h_str, 0.0)
                            
                            # Construct violet color bar rods spanning above or below the zero horizon line
                            hist_bars.append(ft.BarChartGroup(
                                x=idx,
                                bar_rods=[ft.BarChartRod(from_y=0, to_y=avg_gex_val, color=ft.colors.VIOLET, width=10, border_radius=2)]
                            ))
                            
                            # Plot time markers precisely every 3 hours onto the horizontal grid matrix
                            if idx % 3 == 0:
                                hist_labels.append(ft.ChartAxisLabel(value=idx, label=ft.Text(h_str, size=10, color=ft.colors.GREY_400, weight=ft.FontWeight.W_500)))
                        
                        history_change_bar_chart.bar_groups = hist_bars
                        history_change_bottom_axis.labels = hist_labels
            except Exception as ex:
                print(f"Historical Bar Chart Processing Exception: {ex}")

            # --- BAR CHARTS ENGINE ---
            new_groups, abs_groups, breakdown_groups, iv_bar_groups, new_labels, min_dist, spot_index = [], [], [], [], [], float('inf'), -1
            for item in m['chart_data']:
                dist = abs(item['strike'] - m['spot'])
                if dist < min_dist: min_dist, spot_index = dist, item['index']
            
            valid_ivs = [item['iv_skew'] for item in m['chart_data'] if item['iv_skew'] > 0]
            max_iv = max(valid_ivs, default=100.0)
            min_iv = min(valid_ivs, default=0.0)
            
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
                val, abs_val, strike_val, is_spot = item['gex'], item['abs_gex'], item['strike'], (item['index'] == spot_index)
                bull_val, bear_val, iv_val = item['bullish_gex'], item['bearish_gex'], item['iv_skew']
                
                new_groups.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val, color=ft.colors.GREEN_400 if val >= 0 else ft.colors.RED_400, width=12, border_radius=2)]))
                abs_groups.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_val, color=ft.colors.YELLOW, width=12, border_radius=2)]))
                
                breakdown_groups.append(ft.BarChartGroup(
                    x=item['index'],
                    bar_rods=[
                        ft.BarChartRod(from_y=0, to_y=bull_val, color="#FFFFF0", width=6, border_radius=1),
                        ft.BarChartRod(from_y=0, to_y=bear_val, color="#87CEEB", width=6, border_radius=1)
                    ]
                ))
                
                iv_bar_groups.append(ft.BarChartGroup(
                    x=item['index'],
                    bar_rods=[
                        ft.BarChartRod(from_y=floor_y, to_y=iv_val if iv_val > 0 else floor_y, color=ft.colors.ORANGE_700, width=12, border_radius=2)
                    ]
                ))
                
                if strike_val % 2000 == 0:
                    label_color = ft.colors.BLUE_200 if is_spot else ft.colors.GREY_400
                    new_labels.append(ft.ChartAxisLabel(value=item['index'], label=ft.Text(f"{strike_val/1000:.0f}k", size=10, color=label_color, rotate=45, weight=ft.FontWeight.BOLD if is_spot else ft.FontWeight.NORMAL)))
            
            gex_bar_chart.bar_groups = new_groups
            net_axis.labels = new_labels
            
            abs_gex_chart.bar_groups = abs_groups
            abs_axis.labels = new_labels
            
            drop_axis_labels = list(new_labels)
            breakdown_gex_chart.bar_groups = breakdown_groups
            breakdown_axis.labels = drop_axis_labels
            
            iv_skew_bar_chart.bar_groups = iv_bar_groups
            iv_bottom_axis.labels = list(new_labels)
            
            page.update()

    page.add(
        ft.Row([ft.Text("⚡ Deribit GEX Terminal", size=20, weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("Refresh", on_click=refresh_dashboard, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)))], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("BTC UNDERLYING SPOT", size=11, color=ft.colors.GREY_500), spot_txt], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
        create_section_header("NET GAMMA PROFILES BY STRIKE"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart)),
        create_section_header("ABSOLUTE GAMMA EXPOSURE"),
        ft.Card(content=ft.Container(padding=15, content=abs_gex_chart)),
        
        # --- NEW REQUESTED NET GAMMA CHANGE (24H) HISTORICAL CARD PLACEMENT ---
        create_section_header("NET GAMMA CHANGE (24H)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=history_change_bar_chart)),
        
        create_section_header("GAMMA EXPOSURE BREAKDOWN"),
        ft.Card(content=ft.Container(padding=15, content=breakdown_gex_chart)),
        
        create_section_header("TOTAL GAMMA EXPOSURE (3M)"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([
            ui_row_item("Call Gamma", call_gex_txt_3m), ui_row_item("Put Gamma", put_gex_txt_3m), ui_row_item("Net Gamma", net_gex_txt_3m), ui_row_item("Call Weight (%)", weight_txt_3m)
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
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("NET CALL INFLOWS", inflows_call_txt), ui_row_item("NET PUT INFLOWS", outflows_put_txt), ui_row_item("NET PREMIUM BIAS", net_flow_txt)])))
    )
    refresh_dashboard()

if __name__ == "__main__":
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=None)
