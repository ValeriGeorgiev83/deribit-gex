import flet as ft
import requests
import pandas as pd
import os
import math
from datetime import datetime, timezone

# --- DERIBIT DATA ENGINE ---
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
        
        parsed_options.append({
            'strike': strike, 
            'type': option_type, 
            'oi': oi, 
            'gex': gex_value,
            'abs_gex': abs(gex_value),
            'days_to_expiry': days_to_expiry
        })
        
    base_df = pd.DataFrame(parsed_options)
    if base_df.empty: return None
    
    df_3d = base_df[base_df['days_to_expiry'] <= 3.0]
    if df_3d.empty: df_3d = base_df[base_df['days_to_expiry'] <= 90.0]

    center_spot_1k = round(spot_price / 1000.0) * 1000
    lower_bound = center_spot_1k - 8000
    upper_bound = center_spot_1k + 8000
    
    df_chart = df_3d[(df_3d['strike'] >= lower_bound) & (df_3d['strike'] <= upper_bound)].copy()
    df_chart['strike_bucket'] = df_chart['strike'].apply(lambda x: round(x / 1000.0) * 1000)
    
    grouped = df_chart.groupby('strike_bucket').agg({'gex': 'sum', 'abs_gex': 'sum'})
    target_buckets = list(range(int(lower_bound), int(upper_bound) + 1000, 1000))
    
    net_chart = []
    abs_chart = []
    for idx, b_strike in enumerate(target_buckets):
        net_chart.append({"index": idx, "strike": b_strike, "gex": grouped.get('gex', {}).get(b_strike, 0.0)})
        abs_chart.append({"index": idx, "strike": b_strike, "abs_gex": grouped.get('abs_gex', {}).get(b_strike, 0.0)})

    return {"spot": spot_price, "net_chart": net_chart, "abs_chart": abs_chart}

# --- UI LOGIC ---
def main(page: ft.Page):
    page.title = "Deribit DEX Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    
    gex_bar_chart = ft.BarChart(bar_groups=[], height=200, bottom_axis=ft.ChartAxis(labels_size=24))
    
    # NEW ABS GEX LINE CHART
    abs_gex_chart = ft.LineChart(
        data_series=[],
        border=ft.Border(bottom=ft.BorderSide(1, ft.colors.GREY_800)),
        height=200,
        min_y=0,
        animate=300
    )

    def refresh_dashboard(e=None):
        m = fetch_deribit_gex("BTC")
        if m:
            # Update Bar Chart
            gex_bar_chart.bar_groups = [
                ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(to_y=item['gex'], color=ft.colors.GREEN_400 if item['gex'] >= 0 else ft.colors.RED_400, width=12)])
                for item in m['net_chart']
            ]
            
            # Update Abs GEX Line Chart
            abs_gex_chart.data_series = [
                ft.LineChartData(
                    data_points=[ft.LineChartDataPoint(i['index'], i['abs_gex']) for i in m['abs_chart']],
                    color=ft.colors.YELLOW,
                    curved=True,
                    below_line_bgcolor=ft.colors.with_opacity(0.2, ft.colors.YELLOW),
                    stroke_width=3
                )
            ]
            page.update()

    page.add(
        ft.Text("Deribit DEX Terminal", size=20, weight=ft.FontWeight.BOLD),
        ft.ElevatedButton("Refresh", on_click=refresh_dashboard),
        ft.Text("NET GAMMA", size=13, weight=ft.FontWeight.BOLD, color=ft.colors.GREY_500),
        ft.Card(content=ft.Container(gex_bar_chart, padding=10)),
        ft.Text("ABS GEX", size=13, weight=ft.FontWeight.BOLD, color=ft.colors.GREY_500),
        ft.Card(content=ft.Container(abs_gex_chart, padding=10))
    )
    refresh_dashboard()

ft.app(target=main)
