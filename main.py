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
        if option_type == 'P':
            gex_value = -gex_value
            
        parsed_options.append({'strike': strike, 'type': option_type, 'oi': oi, 'volume': volume, 'gex': gex_value})
        
    df = pd.DataFrame(parsed_options)
    if df.empty: return None
        
    total_gex = df['gex'].sum()
    call_oi = df[df['type'] == 'C']['oi'].sum()
    put_oi = df[df['type'] == 'P']['oi'].sum()
    cp_ratio = put_oi / call_oi if call_oi > 0 else 0
    
    call_vol = df[df['type'] == 'C']['volume'].sum()
    put_vol = df[df['type'] == 'P']['volume'].sum()
    
    grouped_gex = df.groupby('strike')['gex'].sum().sort_index()
    flip_level = spot_price
    for i in range(len(grouped_gex) - 1):
        if (grouped_gex.iloc[i] < 0 and grouped_gex.iloc[i+1] > 0) or (grouped_gex.iloc[i] > 0 and grouped_gex.iloc[i+1] < 0):
            flip_level = grouped_gex.index[i]
            break

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

    return {
        "spot": spot_price, "net_gamma": total_gex, "flip": flip_level,
        "max_pain": max_pain_level, "cp_ratio": cp_ratio, "call_vol": call_vol, "put_vol": put_vol
    }

def main(page: ft.Page):
    page.title = "GEX Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 16

    # BULLETPROOF FIX: Swapped out the old module references for simple string colors
    spot_txt = ft.Text("$0.00", size=24, weight=ft.FontWeight.BOLD, color="blue400")
    gex_txt = ft.Text("0.00 BTC", size=24, weight=ft.FontWeight.BOLD)
    flip_txt = ft.Text("$0.00", size=24, weight=ft.FontWeight.BOLD, color="orange400")
    pain_txt = ft.Text("$0.00", size=24, weight=ft.FontWeight.BOLD, color="red400")
    cp_ratio_txt = ft.Text("0.00", size=24, weight=ft.FontWeight.BOLD)
    inflows_txt = ft.Text("Loading...", size=16, color="grey400")

    def ui_card(title, component):
        return ft.Card(
            content=ft.Container(
                content=ft.Column([
                    ft.Text(title, size=12, color="grey500", weight=ft.FontWeight.BOLD),
                    component
                ], spacing=4),
                padding=14
            )
        )

    def refresh_dashboard(e=None):
        metrics = fetch_deribit_gex("BTC")
        if metrics:
            spot_txt.value = f"${metrics['spot']:,.2f}"
            gex_val = metrics['net_gamma']
            gex_txt.value = f"{gex_val:+,.2f} BTC"
            
            # String color updates to prevent future framework update crashes
            gex_txt.color = "green400" if gex_val >= 0 else "red400"
            
            flip_txt.value = f"${metrics['flip']:,.0f}"
            pain_txt.value = f"${metrics['max_pain']:,.0f}"
            cp_ratio_txt.value = f"{metrics['cp_ratio']:.2f}"
            inflows_txt.value = f"🟢 Call Vol: {metrics['call_vol']:,.0f}\n🔴 Put Vol: {metrics['put_vol']:,.0f}"
            page.update()

    page.add(
        ft.Row([
            ft.Text("⚡ Deribit GEX Mobile", size=20, weight=ft.FontWeight.BOLD),
            ft.IconButton(icon=ft.icons.REFRESH, on_click=refresh_dashboard, icon_color="greenaccent")
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ui_card("BTC INDEX SPOT PRICE", spot_txt),
        ui_card("NET CUMULATIVE GEX", gex_txt),
        ui_card("GAMMA FLIP ZONE", flip_txt),
        ui_card("MAX PAIN LEVEL", pain_txt),
        ui_card("C/P OI RATIO", cp_ratio_txt),
        ui_card("24H VOLUME INFLOWS", inflows_txt)
    )
    refresh_dashboard()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    ft.app(target=main, port=port, host="0.0.0.0")
