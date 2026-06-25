import flet as ft
import requests
import pandas as pd
import os
import math
from datetime import datetime, timezone

def fetch_deribit_gex(currency="BTC"):
    try:
        # 1. Fetch live index spot price
        idx_url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={currency.lower()}_usd"
        idx_res = requests.get(idx_url).json()
        spot_price = float(idx_res['result']['index_price'])
        
        # 2. Fetch options market summary
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
            
        expiry_str = parts[1] # e.g. "26JUN26"
        strike = float(parts[2])
        option_type = parts[3]
        oi = float(item.get('open_interest', 0))
        volume = float(item.get('volume', 0))
        
        try:
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            expiry_dt = expiry_dt.replace(hour=8, minute=0, second=0)
            days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            
            # Skip contract if it's already expired
            if days_to_expiry < 0:
                continue
        except Exception:
            continue
            
        iv = float(item.get('mark_iv', 50)) / 100.0
        if iv == 0: iv = 0.5
            
        # Black-Scholes Gamma Approximation curve mapping
        try:
            t_days = max(days_to_expiry, 0.01) / 365.0
            distance = abs(math.log(spot_price / strike))
            approx_gamma = (1.0 / (iv * math.sqrt(t_days) * math.sqrt(2 * math.pi))) * math.exp(-0.5 * (distance / (iv * math.sqrt(t_days)))**2) / spot_price
        except Exception:
            approx_gamma = 0.0001 / max(1.0, abs(spot_price - strike))

        # Gamma exposure calculation: Calls (+), Puts (-)
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
    
    # --- SPLIT DATA FRAMES BY TIMEFRAME REQUIREMENTS ---
    df_3m = base_df[base_df['days_to_expiry'] <= 90.0]  # Expirations max 3 months out
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]   # Expirations max 3 days out
    
    # Fallback to 3m data array if the ultra short-term framework returns blank
    if df_3d.empty: 
        df_3d = df_3m

    # --- SECTION 1: TOTAL GAMMA EXPOSURE (<= 3 Months) ---
    call_df_3m = df_3m[df_3m['type'] == 'C']
    put_df_3m = df_3m[df_3m['type'] == 'P']
    
    call_gex = call_df_3m['gex'].sum()
    put_gex = put_df_3m['gex'].sum()
    net_gex = call_gex + put_gex
    
    total_abs_gex = abs(call_gex) + abs(put_gex)
    call_weight_pct = (abs(call_gex) / total_abs_gex * 100) if total_abs_gex > 0 else 50.0
    
    # --- SECTION 2: IMPORTANT LEVELS (<= 3 Days Only) ---
    call_df_3d = df_3d[df_3d['type'] == 'C']
    put_df_3d = df_3d[df_3d['type'] == 'P']
    
    # True Max Pain calculation
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

    # Gamma Flip Zone calculation
    grouped_gex_3d = df_3d.groupby('strike')['gex'].sum().sort_index()
    flip_level = spot_price
    for i in range(len(grouped_gex_3d) - 1):
        if (grouped_gex_3d.iloc[i] < 0 and grouped_gex_3d.iloc[i+1] > 0) or (grouped_gex_3d.iloc[i] > 0 and grouped_gex_3d.iloc[i+1] < 0):
            flip_level = grouped_gex_3d.index[i]
            break

    # Support & Resistance walls
    call_strike_gex_3d = call_df_3d.groupby('strike')['gex'].sum()
    put_strike_gex_3d = put_df_3d.groupby('strike')['gex'].sum().abs()
    
    resistance_level = call_strike_gex_3d.idxmax() if not call_strike_gex_3d.empty else spot_price * 1.02
