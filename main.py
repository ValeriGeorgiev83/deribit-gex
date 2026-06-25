import flet as ft
import requests
import pandas as pd
import os

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

    parsed_options = []
    for item in data_list:
        name = item['instrument_name']
        parts = name.split('-')
        if len(parts) < 4: continue
            
        strike = float(parts[2])
        option_type = parts[3]
        oi = float(item.get('open_interest', 0))
        volume = float(item.get('volume', 0))
        gamma = float(item.get('gamma', 0)) if item.get('gamma') is not None else 0.0
        
        gex_value = oi * gamma * spot_price
            
        parsed_options.append({
            'strike': strike, 
            'type': option_type, 
            'oi': oi, 
            'volume': volume, 
            'gex': gex_value
        })
        
    df = pd.DataFrame(parsed_options)
    if df.empty: return None
    
    # --- SECTION 1: CUMULATIVE GAMMA METRICS ---
    call_df = df[df['type'] == 'C']
    put_df = df[df['type'] == 'P']
    
    call_gex = call_df['gex'].sum()
    put_gex = -put_df['gex'].sum()
    net_gex = call_gex + put_gex
    
    total_abs_gex = abs(call_gex) + abs(put_gex)
    call_weight_pct = (abs(call_gex) / total_abs_gex * 100) if total_abs_gex > 0 else 0
    
    # --- SECTION 2: INSTITUTIONAL LEVEL ANALYSIS ---
    strikes = df['strike'].unique()
    min_pain = float('inf')
    max_pain_level = spot_price
    for s in strikes:
        pain = 0
        for _, row in df.iterrows():
            if row['type'] == 'C' and row['strike'] < s: pain += (s - row['strike']) * row['oi']
            elif row['type'] == 'P' and row['strike'] > s: pain += (row['strike'] - s) * row['oi']
        if pain < min_pain:
            min_pain = pain
            max_pain_level = s

    df['net_strike_gex'] = df.apply(lambda r: r['gex'] if r['type'] == 'C' else -r['gex'], axis=1)
    grouped_gex = df.groupby('strike')['net_strike_gex'].sum().sort_index()
    flip_level = spot_price
    for i in range(len(grouped_gex) - 1):
        if (grouped_gex.iloc[i] < 0 and grouped_gex.iloc[i+1] > 0) or (grouped_gex.iloc[i] > 0 and grouped_gex.iloc[i+1] < 0):
            flip_level = grouped_gex.index[i]
            break

    call_strike_gex = call_df.groupby('strike')['gex'].sum()
    resistance_level = call_strike_gex.idxmax() if not call_strike_gex.empty else spot_price * 1.05
    
    put_strike_profiles = put_df.groupby('strike')['gex'].sum()
    support_level = put_strike_profiles.idxmax() if not put_strike_profiles.empty else spot_price * 0.95
    
    breakout_price = resistance_level * 1.01

    # --- SECTION 3: INFLOW ANALYSIS ---
    call_vol = call_df['volume'].sum()
    put_vol = put_df['volume'].sum()
    call_oi = call_df['oi'].sum()
    put_oi = put_df['oi'].sum()
    cp_ratio = put_oi / call_oi if call_oi > 0 else 0

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
        "call_vol": call_vol,
        "put_vol": put_vol,
        "cp_ratio": cp_ratio
    }

def fmt_gex(val):
    sign = "+" if val >= 0 else "-"
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{sign}{abs_val/1000:.1f}k"
    return f"{sign}{abs_val:.1f}"

def main(page: ft.Page):
    page.title = "GEX Advanced Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14

    spot_txt = ft.Text("$0.00", size=22, weight=ft.FontWeight.BOLD, color="blue400")
    
    call_gex_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color="green400")
    put_gex_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color="red400")
    net_gex_txt = ft.Text("0.0k", size=22, weight=ft.FontWeight.BOLD)
    weight_txt = ft.Text("0.0%", size=18, weight=ft.FontWeight.W_600, color="blue300")
    
    pain_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600)
    flip_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color="orange400")
    breakout_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color="greenaccent")
    res_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color="purple300")
    sup_txt = ft.Text("$0.00", size=18, weight=ft.FontWeight.W_600, color="pink400")
    
    inflows_call_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600, color="green400")
    inflows_put_txt = ft.Text("0.0k", size=18, weight=ft.
