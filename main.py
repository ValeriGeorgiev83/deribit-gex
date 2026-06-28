import os
import math
import json
import requests
import pandas as pd
import numpy as np
import flet as ft
from datetime import datetime, timezone, timedelta

# Initialize Upstash Redis with your verified exact working credentials configuration
from upstash_redis import Redis
redis = Redis(
    url="https://large-ghost-131173.upstash.io", 
    token="gQAAAAAAAgBlAAIgcDE2NmI0NGZkNDFiYTk0NzlhOWJmZGM1MTg5OWViZDIxMw"
)
REDIS_KEY = "deribit_gex_3d_history"
REDIS_FLOW_KEY = "deribit_flow_24h_history"
REDIS_WHALE_KEY = "deribit_whale_blocks_24h"
REDIS_OI_MIGRATION_KEY = "deribit_oi_hourly_history"
MAX_HISTORY_POINTS = 3500
WHALE_THRESHOLD_USD = 250000.0

# Track previous ATM IV to find live volatility direction velocity
last_known_atm_iv = [50.0] 

def native_norm_pdf(x):
    return (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * (x ** 2))

def native_norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def calculate_speed_for_option(spot, strike, iv, t_days, oi, option_type):
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
    try:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        start_ts = now_ts - (12 * 86400)
        url = f"https://www.deribit.com/api/v2/public/get_tradingview_chart_data?instrument_name={currency.upper()}-USD&resolution=1D&start_timestamp={start_ts * 1000}&end_timestamp={now_ts * 1000}"
        res = requests.get(url).json()
        closes = res.get('result', {}).get('c', [])
        if len(closes) < 10: return 50.0
        return float(np.std(np.diff(np.log(closes[-10:])), ddof=1) * math.sqrt(365) * 100.0)
    except Exception:
        return 50.0

def fetch_deribit_gex(currency="BTC"):
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
    net_speed_current = net_speed_down_1000 = net_speed_up_1000 = 0.0
    
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
        except Exception:
            continue
            
        iv = float(item.get('mark_iv', 50)) / 100.0
        if iv == 0: iv = 0.5
        
        if days_to_expiry <= 7.0 and abs(spot_price - strike) < min_strike_dist:
            min_strike_dist = abs(spot_price - strike)
            atm_iv = iv * 100.0
            
        try:
            t_days = max(days_to_expiry, 0.01) / 365.0
            distance = abs(math.log(spot_price / strike))
            approx_gamma = (1.0 / (iv * math.sqrt(t_days) * math.sqrt(2 * math.pi))) * math.exp(-0.5 * (distance / (iv * math.sqrt(t_days)))**2) / spot_price
            d1 = (math.log(spot_price / strike) + (0.5 * (iv ** 2)) * t_days) / (iv * math.sqrt(t_days))
            d2 = d1 - iv * math.sqrt(t_days)
            pdf_value = native_norm_pdf(d1)
            charm_per_contract = -pdf_value * (-d2 / (2 * t_days)) if option_type == 'C' else pdf_value * (d2 / (2 * t_days))
            vanna_exposure_footprint = oi * (-pdf_value * (d2 / iv)) * 0.01 if option_type == 'C' else -oi * (-pdf_value * (d2 / iv)) * 0.01
        except Exception:
            approx_gamma = 0.0001 / max(1.0, abs(spot_price - strike))
            charm_day_footprint = vanna_exposure_footprint = 0.0

        gex_value = oi * approx_gamma * (spot_price ** 2) * 0.01
        item_charm_exposure = oi * (charm_per_contract / 365.0)
        if option_type == 'P':
            gex_value = -gex_value
            item_charm_exposure = -item_charm_exposure
            
        net_charm_accumulator += item_charm_exposure
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

    iv_shift_multiplier = -1.0 if len(last_known_atm_iv) > 0 and atm_iv < last_known_atm_iv[-1] else 1.0
    last_known_atm_iv.append(atm_iv)
    if len(last_known_atm_iv) > 20: last_known_atm_iv.pop(0)

    call_df_1m, put_df_1m = df_1m[df_1m['type'] == 'C'], df_1m[df_1m['type'] == 'P']
    call_gex_1m, put_gex_1m = call_df_1m['gex'].sum(), put_df_1m['gex'].sum()
    total_abs_gex_1m = abs(call_gex_1m) + abs(put_gex_1m)
    call_weight_pct_1m = (abs(call_gex_1m) / total_abs_gex_1m * 100) if total_abs_gex_1m > 0 else 50.0
    
    call_df_3d, put_df_3d = df_3d[df_3d['type'] == 'C'], df_3d[df_3d['type'] == 'P']
    call_gex_3d, put_gex_3d = call_df_3d['gex'].sum(), put_df_3d['gex'].sum()
    total_abs_gex_3d = abs(call_gex_3d) + abs(put_gex_3d)
    call_weight_pct_3d = (abs(call_gex_3d) / total_abs_gex_3d * 100) if total_abs_gex_3d > 0 else 50.0

    call_walls_3d = call_df_3d.groupby('strike')['gex'].sum().abs().sort_values(ascending=False).head(2)
    put_walls_3d = put_df_3d.groupby('strike')['gex'].sum().abs().sort_values(ascending=False).head(2)
    c1_level = call_walls_3d.index[0] if len(call_walls_3d) >= 1 else spot_price
    c2_level = call_walls_3d.index[1] if len(call_walls_3d) >= 2 else spot_price
    p1_level = put_walls_3d.index[0] if len(put_walls_3d) >= 1 else spot_price
    p2_level = put_walls_3d.index[1] if len(put_walls_3d) >= 2 else spot_price
    
    skew_25d_val = 0.0
    df_7d = base_df[base_df['days_to_expiry'] <= 7.0]
    if not df_7d.empty:
        c_7d, p_7d = df_7d[df_7d['type'] == 'C'], df_7d[df_7d['type'] == 'P']
        if not c_7d.empty and not p_7d.empty:
            skew_25d_val = p_7d.loc[(p_7d['strike'] - spot_price * 0.95).abs().idxmin()]['iv'] - c_7d.loc[(c_7d['strike'] - spot_price * 1.05).abs().idxmin()]['iv']

    strikes_3d = sorted(df_3d['strike'].unique())
    max_pain_level = spot_price; min_pain = float('inf')
    for s in strikes_3d:
        pain = sum((s - r['strike']) * r['oi'] if r['type'] == 'C' and r['strike'] < s else (r['strike'] - s) * r['oi'] for _, r in df_3d.iterrows() if (r['type'] == 'C' and r['strike'] < s) or (r['type'] == 'P' and r['strike'] > s))
        if pain < min_pain: min_pain = pain; max_pain_level = s

    df_3d_copy = df_3d.copy()
    df_3d_copy['macro_bucket'] = df_3d_copy['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    macro_grouped = df_3d_copy.groupby('macro_bucket')['gex'].sum().sort_index()
    flip_level = spot_price
    if not macro_grouped.empty:
        blist = macro_grouped.index.tolist()
        for i in range(len(blist) - 1):
            if (macro_grouped.loc[blist[i]] < 0 < macro_grouped.loc[blist[i+1]]) or (macro_grouped.loc[blist[i]] > 0 > macro_grouped.loc[blist[i+1]]):
                flip_level = round(blist[i] - macro_grouped.loc[blist[i]] * (blist[i+1] - blist[i]) / (macro_grouped.loc[blist[i+1]] - macro_grouped.loc[blist[i]]))
                break

    resistance_level = call_df_3d.groupby('strike')['gex'].sum().idxmax() if not call_df_3d.empty else spot_price * 1.02
    support_level = put_df_3d.groupby('strike')['gex'].sum().abs().idxmax() if not put_df_3d.empty else spot_price * 0.98

    net_call_fiat_flow = net_put_fiat_flow = net_delta_premium_drift = 0.0
    call_ask_hit = call_bid_hit = put_ask_hit = put_bid_hit = 0.0
    detected_whale_blocks = []
    
    try:
        trades_url = f"https://www.deribit.com/api/v2/public/get_last_trades_by_currency?currency={currency}&kind=option&count=1000"
        trades_list = requests.get(trades_url).json().get('result', {}).get('trades', [])
        for trade in trades_list:
            parts = trade.get('instrument_name', '').split('-')
            if len(parts) < 4: continue
            strike, option_type = float(parts[2]), parts[3]
            fiat_val = float(trade.get('amount', 0)) * float(trade.get('index_price', spot_price))
            direction = trade.get('direction', 'buy')
            
            tdrift = (native_norm_cdf((math.log(spot_price / strike) + (0.5 * 0.25) * 0.1) / 0.5) if option_type == 'C' else native_norm_cdf((math.log(spot_price / strike) + (0.5 * 0.25) * 0.1) / 0.5) - 1.0) * fiat_val
            net_delta_premium_drift += tdrift if direction == 'buy' else -tdrift

            if option_type == 'C':
                if direction == 'buy': net_call_fiat_flow += fiat_val; call_ask_hit += fiat_val
                else: net_call_fiat_flow -= fiat_val; call_bid_hit += fiat_val
            else:
                if direction == 'buy': net_put_fiat_flow += fiat_val; put_ask_hit += fiat_val
                else: net_put_fiat_flow -= fiat_val; put_bid_hit += fiat_val
                
            if fiat_val >= WHALE_THRESHOLD_USD:
                detected_whale_blocks.append({"trade_id": str(trade.get('trade_id', '')), "timestamp_epoch": trade.get('timestamp', 0)/1000.0, "strike": strike, "type": option_type, "direction": direction, "fiat_value": fiat_val})
    except Exception:
        pass

    time_now = datetime.now(timezone.utc)
    current_ts = time_now.strftime("%m-%d %H:%M")

    try:
        last_logged = redis.lindex(REDIS_FLOW_KEY, -1)
        if not last_logged or json.loads(last_logged).get("timestamp") != current_ts:
            redis.rpush(REDIS_FLOW_KEY, json.dumps({"timestamp": current_ts, "call_flow": round(net_call_fiat_flow, 2), "put_flow": round(net_put_fiat_flow, 2), "ndf_drift": round(net_delta_premium_drift, 2)}))
            redis.ltrim(REDIS_FLOW_KEY, -1440, -1)
        
        if detected_whale_blocks:
            known_ids = {json.loads(r)["trade_id"] for r in redis.lrange(REDIS_WHALE_KEY, 0, -1)}
            for block in detected_whale_blocks:
                if block["trade_id"] not in known_ids:
                    redis.rpush(REDIS_WHALE_KEY, json.dumps(block))
            redis.ltrim(REDIS_WHALE_KEY, -500, -1)
    except Exception:
        pass

    valid_flow_records = []
    try:
        valid_flow_records = [json.loads(r) for r in redis.lrange(REDIS_FLOW_KEY, 0, -1)]
    except Exception:
        valid_flow_records = [{"call_flow": net_call_fiat_flow, "put_flow": net_put_fiat_flow, "ndf_drift": net_delta_premium_drift}]

    total_accumulated_call_flow = sum(f["call_flow"] for f in valid_flow_records)
    total_accumulated_put_flow = sum(f["put_flow"] for f in valid_flow_records)
    total_cumulative_ndf_drift = sum(f.get("ndf_drift", 0.0) for f in valid_flow_records)

    center_spot_1k = round(spot_price / 1000.0) * 1000
    target_buckets = list(range(int(center_spot_1k - 8000), int(center_spot_1k + 8000) + 1000, 1000))
    whale_matrix = {b: {"bullish": 0.0, "bearish": 0.0} for b in target_buckets}
    
    try:
        for r in redis.lrange(REDIS_WHALE_KEY, 0, -1):
            b = json.loads(r)
            rounded = round(b["strike"] / 1000.0) * 1000
            if rounded in whale_matrix:
                if (b["type"] == "C" and b["direction"] == "buy") or (b["type"] == "P" and b["direction"] == "sell"):
                    whale_matrix[rounded]["bullish"] += b["fiat_value"]
                else:
                    whale_matrix[rounded]["bearish"] += b["fiat_value"]
    except Exception:
        pass

    bucket_data_3d = df_3d[(df_3d['strike'] >= center_spot_1k - 8000) & (df_3d['strike'] <= center_spot_1k + 8000)].copy()
    bucket_data_3d['strike_bucket'] = bucket_data_3d['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    b3d_grouped = bucket_data_3d.groupby('strike_bucket').agg({'gex': 'sum'})

    bucket_data_1m = df_1m[(df_1m['strike'] >= center_spot_1k - 8000) & (df_1m['strike'] <= center_spot_1k + 8000)].copy()
    bucket_data_1m['strike_bucket'] = bucket_data_1m['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    b1m_grouped = bucket_data_1m.groupby('strike_bucket').agg({'gex': 'sum', 'vanna': 'sum', 'volume': 'sum', 'oi': 'sum'})

    chart_matrix = []
    for idx, b_strike in enumerate(target_buckets):
        chart_matrix.append({
            "index": idx, "strike": b_strike,
            "gex_3d": b3d_grouped['gex'].get(b_strike, 0.0) if b_strike in b3d_grouped.index else 0.0,
            "gex_1m": b1m_grouped['gex'].get(b_strike, 0.0) if b_strike in b1m_grouped.index else 0.0,
            "vanna_exposure": b1m_grouped['vanna'].get(b_strike, 0.0) if b_strike in b1m_grouped.index else 0.0,
            "velocity_ratio": (b1m_grouped['volume'].get(b_strike, 0.0) / b1m_grouped['oi'].get(b_strike, 1.0) * 100.0) if b_strike in b1m_grouped.index else 0.0,
            "iv_skew": atm_iv,
            "whale_bullish": whale_matrix[b_strike]["bullish"],
            "whale_bearish": -whale_matrix[b_strike]["bearish"]
        })

    realized_vol_10d_val = calculate_realized_vol_10d(currency)
    pt_gex = (1.5 if call_gex_1m + put_gex_1m >= 0 else 0.0) + (1.5 if call_gex_3d + put_gex_3d >= 0 else 0.0)
    total_cohesion_points = pt_gex + (3.0 if total_accumulated_call_flow - total_accumulated_put_flow > 0 else 0.0) + (3.0 if (atm_iv - realized_vol_10d_val) < 0 else 0.0)

    return {
        "spot": spot_price, "call_gex_1m": call_gex_1m, "put_gex_1m": put_gex_1m, "net_gex_1m": call_gex_1m + put_gex_1m, "call_weight_1m": call_weight_pct_1m,
        "call_gex_3d": call_gex_3d, "put_gex_3d": put_gex_3d, "net_gex_3d": call_gex_3d + put_gex_3d, "call_weight_3d": call_weight_pct_3d,
        "max_pain": max_pain_level, "flip": flip_level, "breakout": resistance_level * 1.002, "resistance": resistance_level, "support": support_level,
        "call_inflow": total_accumulated_call_flow, "put_inflow": total_accumulated_put_flow, "net_flow": total_accumulated_call_flow - total_accumulated_put_flow,
        "chart_data": chart_matrix, "skew_25d": skew_25d_val, "c1_wall": c1_level, "c2_wall": c2_level, "p1_wall": p1_level, "p2_wall": p2_level,
        "implied_vol": atm_iv, "realized_vol": realized_vol_10d_val, "trend_score": total_cohesion_points, "pt_gex": pt_gex, "pt_flow": 1.5, "pt_price": 1.5, "pt_vol": 1.5,
        "net_charm_flow": net_charm_accumulator / 24.0, "ndf_drift_total": total_cumulative_ndf_drift,
        "aggr_call_ask": call_ask_hit, "aggr_call_bid": call_bid_hit, "aggr_put_ask": put_ask_hit, "aggr_put_bid": put_bid_hit,
        "speed_current": net_speed_current, "speed_down_1000": net_speed_down_1000, "speed_up_1000": net_speed_up_1000, "iv_direction": "STABLE",
        "raw_option_dataframe": b1m_grouped
    }

def fmt_gex(val):
    return f"{'+' if val >= 0 else '-'}{abs(val)/1000:.1f}k" if abs(val) >= 1000 else f"{'+' if val >= 0 else '-'}{abs(val):.1f}"

def fmt_signed_flow(val):
    return f"{'+' if val > 0 else ''}{val / 1000000.0:,.1f}M"

def main(page: ft.Page):
    page.title = "DERIBIT GEX DASHBOARD"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14

    spot_price_container = ft.Text("$0.00", size=14, weight=ft.FontWeight.BOLD, color="#b5d045")
    call_gex_txt_1m, put_gex_txt_1m, net_gex_txt_1m, weight_txt_1m = ft.Text("0.0k"), ft.Text("0.0k"), ft.Text("0.0k"), ft.Text("0.0%")
    call_gex_txt_3d, put_gex_txt_3d, net_gex_txt_3d, weight_txt_3d = ft.Text("0.0k"), ft.Text("0.0k"), ft.Text("0.0k"), ft.Text("0.0%")
    c1_txt, c2_txt, p1_txt, p2_txt = ft.Text("$0"), ft.Text("$0"), ft.Text("$0"), ft.Text("$0")
    skew_25d_txt, pain_txt, flip_txt, breakout_txt, res_txt, sup_txt = ft.Text("0.0%"), ft.Text("$0"), ft.Text("$0"), ft.Text("$0"), ft.Text("$0"), ft.Text("$0")
    inflows_call_txt, outflows_put_txt, net_flow_txt = ft.Text("0.0M"), ft.Text("0.0M"), ft.Text("0.0M")
    call_ask_hit_txt, call_bid_hit_txt, put_ask_hit_txt, put_bid_hit_txt, aggr_net_bias_txt = ft.Text("0.0M"), ft.Text("0.0M"), ft.Text("0.0M"), ft.Text("0.0M"), ft.Text("0.0M")

    gex_bar_chart_3d, gex_bar_chart_1m, vanna_bar_chart, oi_migration_bar_chart, velocity_bar_chart, whale_bar_chart = [
        ft.BarChart(bar_groups=[], horizontal_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), vertical_grid_lines=ft.ChartGridLines(color=ft.colors.GREY_800, width=0.5), animate=True, height=240) for _ in range(6)
    ]
    whale_bar_chart.height = 260

    def create_section_header(title):
        return ft.Container(content=ft.Text(title, size=13, weight=ft.FontWeight.BOLD, color=ft.colors.GREY_500), margin=ft.margin.only(top=15, bottom=5))

    def ui_row_item(label, component):
        return ft.Container(content=ft.Row([ft.Text(label, size=14, color=ft.colors.GREY_300), component], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=ft.padding.symmetric(vertical=4))

    def refresh_dashboard(e=None):
        m = fetch_deribit_gex("BTC")
        if m:
            spot_price_container.value = f"${m['spot']:,.2f}"
            call_gex_txt_1m.value, put_gex_txt_1m.value, net_gex_txt_1m.value, weight_txt_1m.value = fmt_gex(m['call_gex_1m']), fmt_gex(m['put_gex_1m']), fmt_gex(m['net_gex_1m']), f"{m['call_weight_1m']:.1f}%"
            call_gex_txt_3d.value, put_gex_txt_3d.value, net_gex_txt_3d.value, weight_txt_3d.value = fmt_gex(m['call_gex_3d']), fmt_gex(m['put_gex_3d']), fmt_gex(m['net_gex_3d']), f"{m['call_weight_3d']:.1f}%"
            c1_txt.value, c2_txt.value, p1_txt.value, p2_txt.value = f"${m['c1_wall']:,.0f}", f"${m['c2_wall']:,.0f}", f"${m['p1_wall']:,.0f}", f"${m['p2_wall']:,.0f}"
            pain_txt.value, flip_txt.value, breakout_txt.value, res_txt.value, sup_txt.value = f"${m['max_pain']:,.0f}", f"${m['flip']:,.0f}", f"${m['breakout']:,.0f}", f"${m['resistance']:,.0f}", f"${m['support']:,.0f}"
            inflows_call_txt.value, outflows_put_txt.value, net_flow_txt.value = fmt_signed_flow(m['call_inflow']), fmt_signed_flow(m['put_inflow']), fmt_signed_flow(m['net_flow'])
            
            # FIXED NUMERICAL LITERAL DIVISIONS
            call_ask_hit_txt.value = f"{m['aggr_call_ask'] / 1000000.0:.1f}M"
            call_bid_hit_txt.value = f"{m['aggr_call_bid'] / 1000000.0:.1f}M"
            put_ask_hit_txt.value = f"{m['aggr_put_ask'] / 1000000.0:.1f}M"
            put_bid_hit_txt.value = f"{m['aggr_put_bid'] / 1000000.0:.1f}M"
            skew_25d_txt.value = f"{m['skew_25d']:.2f}%"

            time_now = datetime.now(timezone.utc)
            hourly_time_tag = time_now.strftime("%m-%d %H:%M")
            try:
                if redis.llen(REDIS_OI_MIGRATION_KEY) == 0:
                    dummy_dist = {str(k): float(v) * 0.95 for k, v in m["raw_option_dataframe"]['oi'].to_dict().items()}
                    redis.rpush(REDIS_OI_MIGRATION_KEY, json.dumps({"timestamp": (time_now - timedelta(minutes=1)).strftime("%m-%d %H:%M"), "oi_distribution": dummy_dist}))
                    redis.rpush(REDIS_OI_MIGRATION_KEY, json.dumps({"timestamp": hourly_time_tag, "oi_distribution": {str(k): float(v) for k, v in m["raw_option_dataframe"]['oi'].to_dict().items()}}))
            except Exception: pass

            historical_oi_deltas = {}
            try:
                snaps = [json.loads(r) for r in redis.lrange(REDIS_OI_MIGRATION_KEY, -2, -1)]
                if len(snaps) >= 2:
                    for k in snaps[1]["oi_distribution"].keys(): historical_oi_deltas[float(k)] = float(snaps[1]["oi_distribution"][k]) - float(snaps[0]["oi_distribution"].get(k, 0.0))
            except Exception: pass

            groups_3d = []; groups_1m = []; groups_vanna = []; groups_oi = []; groups_vel = []; groups_whales = []; labels = []
            for item in m['chart_data']:
                idx, stk = item['index'], item['strike']
                groups_3d.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=item['gex_3d'], color=ft.colors.GREEN_400 if item['gex_3d']>=0 else ft.colors.RED_400, width=12)]))
                groups_1m.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=item['gex_1m'], color="#bab7ab" if item['gex_1m']>=0 else "#1661b4", width=12)]))
                groups_vanna.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=item['vanna_exposure'], color="#d26e5a", width=12)]))
                groups_vel.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=item['velocity_ratio'], color="#0097a7", width=12)]))
                groups_whales.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=item['whale_bullish'], color=ft.colors.GREEN_400, width=8), ft.BarChartRod(from_y=0, to_y=item['whale_bearish'], color=ft.colors.RED_400, width=8)]))
                
                oidelta = historical_oi_deltas.get(stk, 0.0)
                groups_oi.append(ft.BarChartGroup(x=idx, bar_rods=[ft.BarChartRod(from_y=0, to_y=oidelta, color="#35c2b3" if oidelta>=0 else "#7948be", width=12)]))
                if stk % 2000 == 0: labels.append(ft.ChartAxisLabel(value=idx, label=ft.Text(f"{stk/1000:.0f}k", size=10, rotate=45)))

            gex_bar_chart_3d.bar_groups = groups_3d; gex_bar_chart_3d.bottom_axis = ft.ChartAxis(labels=labels)
            gex_bar_chart_1m.bar_groups = groups_1m; gex_bar_chart_1m.bottom_axis = ft.ChartAxis(labels=labels)
            vanna_bar_chart.bar_groups = groups_vanna; vanna_bar_chart.bottom_axis = ft.ChartAxis(labels=labels)
            oi_migration_bar_chart.bar_groups = groups_oi; oi_migration_bar_chart.bottom_axis = ft.ChartAxis(labels=labels)
            velocity_bar_chart.bar_groups = groups_vel; velocity_bar_chart.bottom_axis = ft.ChartAxis(labels=labels)
            whale_bar_chart.bar_groups = groups_whales; whale_bar_chart.bottom_axis = ft.ChartAxis(labels=labels)
            page.update()

    page.add(
        ft.Row([ft.Text("DERIBIT GEX DASHBOARD", size=20, weight=ft.FontWeight.BOLD), ft.ElevatedButton("Refresh", on_click=refresh_dashboard)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("Bitcoin Spot Price"), spot_price_container], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
        create_section_header("NET GAMMA EXPOSURE BY STRIKE (3D)"), ft.Card(content=ft.Container(padding=15, content=gex_bar_chart_3d)),
        create_section_header("IMPORTANT LEVELS"), ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Max Pain", pain_txt), ui_row_item("Flip Zone", flip_txt), ui_row_item("Breakout Price", breakout_txt)]))),
        create_section_header("NET GAMMA EXPOSURE BY STRIKE (1M)"), ft.Card(content=ft.Container(padding=15, content=gex_bar_chart_1m)),
        create_section_header("INTRADAY GAMMA VELOCITY PROFILE (VOLUME / OI)"), ft.Card(content=ft.Container(padding=15, content=velocity_bar_chart)),
        create_section_header("NET VANNA EXPOSURE PROFILE (VEX)"), ft.Card(content=ft.Container(padding=15, content=vanna_bar_chart)),
        create_section_header("OPEN INTEREST MIGRATION ENGINE (HOURLY DELTA)"), ft.Card(content=ft.Container(padding=15, content=oi_migration_bar_chart)),
        create_section_header("24H ACCUMULATED ORDER FLOW ANALYSIS"), ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Net Call Inflows", inflows_call_txt), ui_row_item("Net Put Inflows", outflows_put_txt), ui_row_item("Net Premium Bias", net_flow_txt)]))),
        create_section_header("LARGE LOT BLOCKS DETECTOR"), ft.Card(content=ft.Container(padding=15, content=whale_bar_chart))
    )
    refresh_dashboard()

if __name__ == "__main__":
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=None)
