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
        
        # Parse expiration string and filter to a maximum window of 3 days (72 hours)
        try:
            # Format pattern standard for Deribit: DDMMMYY (e.g., 26JUN26)
            expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(tzinfo=timezone.utc)
            # Set to 08:00 UTC as Deribit options settle daily at 08:00 UTC
            expiry_dt = expiry_dt.replace(hour=8, minute=0, second=0)
            
            days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
            
            # CRITICAL FILTER: Only include contracts expiring in <= 3 days, and exclude already expired
            if days_to_expiry > 3.0 or days_to_expiry < 0:
                continue
        except Exception:
            continue # Skip if date format string parse fails
            
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
            'gex': gex_value
        })
        
    df = pd.DataFrame(parsed_options)
    if df.empty: return None
    
    # --- SECTION 1: TOTAL GAMMA EXPOSURE ---
    call_df = df[df['type'] == 'C']
    put_df = df[df['type'] == 'P']
    
    call_gex = call_df['gex'].sum()
    put_gex = put_df['gex'].sum()
    net_gex = call_gex + put_gex
    
    total_abs_gex = abs(call_gex) + abs(put_gex)
    call_weight_pct = (abs(call_gex) / total_abs_gex * 100) if total_abs_gex > 0 else 50.0
    
    # --- SECTION 2: IMPORTANT LEVELS ---
    # True Max Pain formula calculation
    strikes = sorted(df['strike'].unique())
    min_pain = float('inf')
    max_pain_level = spot_price
    
    for s in strikes:
        pain = 0
        for _, row in df.iterrows():
            if row['type'] == 'C' and row['strike'] < s: 
                pain += (s - row['strike']) * row['oi']
            elif row['type'] == 'P' and row['strike'] > s: 
                pain += (row['strike'] - s) * row['oi']
        if pain < min_pain:
            min_pain = pain
            max_pain_level = s

    # Gamma Flip Zone calculation via localized zero-bound crossing detection
    grouped_gex = df.groupby('strike')['gex'].sum().sort_index()
    flip_level = spot_price
    for i in range(len(grouped_gex) - 1):
        if (grouped_gex.iloc[i] < 0 and grouped_gex.iloc[i+1] > 0) or (grouped_gex.iloc[i] > 0 and grouped_gex.iloc[i+1] < 0):
            flip_level = grouped_gex.index[i]
            break

    # Support & Resistance identified via largest localized options barriers
    call_strike_gex = call_df.groupby('strike')['gex'].sum()
    put_strike_gex = put_df.groupby('strike')['gex'].sum().abs()
    
    resistance_level = call_strike_gex.idxmax() if not call_strike_gex.empty else spot_price * 1.02
    support_level = put_strike_gex.idxmax() if not put_strike_gex.empty else spot_price * 0.98
    
    # Volatility Breakout trigger point sitting right outside the near-term Call Wall
    breakout_price = resistance_level * 1.002

    # --- SECTION 3: INFLOW ANALYSIS ---
    call_vol = call_df['volume'].sum()
    put_vol = put_df['volume'].sum()
    net_flow = call_vol - put_vol
    
    call_oi_total = call_df['oi'].sum()
    put_oi_total = put_df['oi'].sum()
    cp_ratio = put_oi_total / call_oi_total if call_oi_total > 0 else 0

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
        "net_flow": net_flow,
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
    
    inflows_call_txt = ft.Text("+0.0k", size=18, weight=ft.FontWeight.W_600, color="green400")
    outflows_put_txt = ft.Text("-0.0k", size=18, weight=ft.FontWeight.W_600, color="red400")
    net_flow_txt = ft.Text("0.0k", size=18, weight=ft.FontWeight.W_600)
    cp_ratio_txt = ft.Text("0.00", size=22, weight=ft.FontWeight.BOLD, color="cyan300")

    def create_section_header(title_name):
        return ft.Container(
            content=ft.Text(title_name, size=13, weight=ft.FontWeight.BOLD, color="grey500"),
            margin=ft.margin.only(top=15, bottom=5)
        )

    def ui_row_item(label, component):
        return ft.Container(
            content=ft.Row([
                ft.Text(label, size=14, color="grey300"),
                component
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            padding=ft.padding.symmetric(vertical=4)
        )

    def refresh_dashboard(e=None):
        m = fetch_deribit_gex("BTC")
        if m:
            spot_txt.value = f"${m['spot']:,.2f}"
            
            call_gex_txt.value = fmt_gex(m['call_gex'])
            put_gex_txt.value = fmt_gex(m['put_gex'])
            net_gex_txt.value = fmt_gex(m['net_gex'])
            net_gex_txt.color = "green400" if m['net_gex'] >= 0 else "red400"
            weight_txt.value = f"{m['call_weight']:.1f}%"
            
            pain_txt.value = f"${m['max_pain']:,.0f}"
            flip_txt.value = f"${m['flip']:,.0f}"
            breakout_txt.value = f"${m['breakout']:,.0f}"
            res_txt.value = f"${m['resistance']:,.0f}"
            sup_txt.value = f"${m['support']:,.0f}"
            
            inflows_call_txt.value = f"+{m['call_vol']/1000:.1f}k" if m['call_vol'] >= 1000 else f"+{m['call_vol']:.0f}"
            outflows_put_txt.value = f"-{m['put_vol']/1000:.1f}k" if m['put_vol'] >= 1000 else f"-{m['put_vol']:.0f}"
            
            net_flow_txt.value = fmt_gex(m['net_flow'])
            net_flow_txt.color = "green400" if m['net_flow'] >= 0 else "red400"
            
            cp_ratio_txt.value = f"{m['cp_ratio']:.2f}"
            page.update()

    page.add(
        ft.Row([
            ft.Text("⚡ Deribit 3-Day GEX Terminal", size=20, weight=ft.FontWeight.BOLD),
            ft.IconButton(icon=ft.icons.REFRESH, on_click=refresh_dashboard, icon_color="greenaccent")
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        
        ft.Card(
            content=ft.Container(
                content=ft.Row([ft.Text("BTC UNDERLYING SPOT", size=11, color="grey500"), spot_txt], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                padding=12
            )
        ),
        
        create_section_header("TOTAL GAMMA EXPOSURE (<= 3D)"),
        ft.Card(
            content=ft.Container(
                padding=14,
                content=ft.Column([
                    ui_row_item("Call Gamma", call_gex_txt),
                    ui_row_item("Put Gamma", put_gex_txt),
                    ui_row_item("Net Gamma", net_gex_txt),
                    ui_row_item("Call Weight (%)", weight_txt),
                ])
            )
        ),
        
        create_section_header("TACTICAL LEVELS"),
        ft.Card(
            content=ft.Container(
                padding=14,
                content=ft.Column([
                    ui_row_item("Max Pain", pain_txt),
                    ui_row_item("Flip Zone", flip_txt),
                    ui_row_item("Breakout Price", breakout_txt),
                    ui_row_item("Resistance Level", res_txt),
                    ui_row_item("Support Level", sup_txt),
                ])
            )
        ),
        
        create_section_header("INFLOW ANALYSIS (<= 3D)"),
        ft.Card(
            content=ft.Container(
                padding=14,
                content=ft.Column([
                    ui_row_item("24h Call Inflows (+)", inflows_call_txt),
                    ui_row_item("24h Put Inflows (-)", outflows_put_txt),
                    ui_row_item("Net Volume Bias", net_flow_txt),
                    ui_row_item("C/P Ratio", cp_ratio_txt),
                ])
            )
        )
    )
    refresh_dashboard()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    ft.app(target=main, port=port, host="0.0.0.0")
