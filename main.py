import flet as ft
import requests
import pandas as pd
import os
import math
from datetime import datetime, timezone

# --- FIXED: BULLETPROOF CVD ENGINE WITH API FALLBACKS ---
def fetch_binance_cvd_change(symbol="BTCUSDT", is_futures=False):
    """
    Extracts accurate periodic CVD by reading native taker volume metrics.
    Handles strict string typecasting and symbol formatting requirements.
    """
    # Force uppercase for Binance strictness
    symbol = symbol.upper()
    base_url = "https://fapi.binance.com" if is_futures else "https://api.binance.com"
    endpoint = f"{base_url}/fapi/v1/klines" if is_futures else f"{base_url}/api/v3/klines"
    
    intervals = {"15m": "15m", "1h": "1h", "4h": "4h"}
    results = {"15m": 0.0, "1h": 0.0, "4h": 0.0}
    
    for key, timeframe in intervals.items():
        try:
            params = {"symbol": symbol, "interval": timeframe, "limit": 1}
            res = requests.get(endpoint, params=params).json()
            
            # Verify we got a valid candle list back, not an error dictionary
            if isinstance(res, list) and len(res) > 0 and isinstance(res[0], list):
                candle = res[0]
                total_vol = float(candle[5])        # Index 5: Total Base Asset Volume
                taker_buy_vol = float(candle[9])    # Index 9: Taker Buy Base Asset Volume
                taker_sell_vol = total_vol - taker_buy_vol
                
                results[key] = taker_buy_vol - taker_sell_vol
            else:
                # Fallback to Taker Long/Short data API if Klines fail or return zero
                raise ValueError("Switching to global taker metrics fallback")
                
        except Exception:
            # Global Taker Volume Fallback Engine
            try:
                fallback_url = "https://fapi.binance.com/futures/data/takerlongshortRatio"
                fb_params = {"symbol": symbol, "period": timeframe, "limit": 1}
                fb_res = requests.get(fallback_url, params=fb_params).json()
                if isinstance(fb_res, list) and len(fb_res) > 0:
                    data = fb_res[0]
                    buy_vol = float(data.get('buyVol', 0))
                    sell_vol = float(data.get('sellVol', 0))
                    results[key] = buy_vol - sell_vol
            except Exception:
                results[key] = 0.0
            
    return results

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

    call_vol_3m = call_df_3m['volume'].sum()
    put_vol_3m = put_df_3m['volume'].sum()
    
    signed_call_inflow = call_vol_3m if call_gex >= 0 else -call_vol_3m
    signed_put_inflow = put_vol_3m if put_gex >= 0 else -put_vol_3m
    net_flow = signed_call_inflow - signed_put_inflow
    
    call_oi_3m = call_df_3m['oi'].sum()
    put_oi_3m = put_df_3m['oi'].sum()
    cp_ratio = put_oi_3m / call_oi_3m if call_oi_3m > 0 else 0

    center_spot_1k = round(spot_price / 1000.0) * 1000
    lower_bound = center_spot_1k - 8000
    upper_bound = center_spot_1k + 8000
    
    df_chart_range = df_3d[(df_3d['strike'] >= lower_bound) & (df_3d['strike'] <= upper_bound)].copy()
    if df_chart_range.empty:
        df_chart_range = df_3m[(df_3m['strike'] >= lower_bound) & (df_3m['strike'] <= upper_bound)].copy()
        
    df_chart_range['strike_bucket'] = df_chart_range['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    
    bucket_gex = df_chart_range.groupby('strike_bucket')['gex'].sum()
    target_buckets = list(range(int(lower_bound), int(upper_bound) + 1000, 1000))
    chart_matrix = []
    
    for idx, b_strike in enumerate(target_buckets):
        gex_val = bucket_gex.get(b_strike, 0.0)
        chart_matrix.append({
            "index": idx,
            "strike": b_strike,
            "gex": gex_val
        })

    cvd_spot_data = fetch_binance_cvd_change("BTCUSDT", is_futures=False)
    cvd_futures_data = fetch_binance_cvd_change("BTCUSDT", is_futures=True)

    return {
        "spot": spot_price,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "net_gex": net_gex,
        "call_weight": call_weight_pct,
        "max_pain": max_pain_level,
        "flip": flip_level,
        "breakout": breakout_price,
        "resistance": resistance_level,
        "support": support_level,
        "call_inflow": signed_call_inflow,
        "put_inflow": signed_put_inflow,
        "net_flow": net_flow,
        "cp_ratio": cp_ratio,
        "chart_data": chart_matrix,
        "cvd_spot": cvd_spot_data,
        "cvd_futures": cvd_futures_data
    }

def fmt_gex(val):
    sign = "+" if val >= 0 else "-"
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{sign}{abs_val/1000:.1f}k"
    return f"{sign}{abs_val:.1f}"

def fmt_inflow(val):
    sign = "+" if val >= 0 else ""
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{sign}{val/1000:.1f}k"
    return f"{sign}{val:.0f}"

def fmt_cvd(val):
    sign = "+" if val >= 0 else ""
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{sign}{val/1000:.1f}k"
    return f"{sign}{val:.2f}"

def main(page: ft.Page):
    page.title = "GEX Advanced Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14

    spot_txt = ft.Text("$0.00", size=22, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_400)
    call_gex_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_400)
    put_gex_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color=ft.colors.RED_400)
    net_gex_txt = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color=ft.colors.BLUE_300)
    
    pain_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600)
    flip_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.ORANGE_400)
    breakout_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.GREEN_ACCENT)
    res_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PURPLE_300)
    sup_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color=ft.colors.PINK_400)
    
    inflows_call_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    outflows_put_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    net_flow_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    cp_ratio_txt = ft.Text("0.00", size=22, weight=ft.
