import os
import math
import json
import requests
import pandas as pd
import flet as ft
from datetime import datetime, timezone
from upstash_redis import Redis

# Initialize Upstash Redis
redis = Redis(
    url="https://large-ghost-131173.upstash.io", 
    token="gQAAAAAAAgBlAAIgcDE2NmI0NGZkNDFiYTk0NzlhOWJmZGM1MTg5OWViZDIxMw"
)
REDIS_KEY = "deribit_gex_3d_history"
MAX_HISTORY_POINTS = 2500

def fetch_deribit_gex(currency="BTC"):
    """Fetches and calculates GEX and market flow data from Deribit."""
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
    
    call_gex_3d = df_3d[df_3d['type'] == 'C']['gex'].sum()
    put_gex_3d = df_3d[df_3d['type'] == 'P']['gex'].sum()
    net_gex_3d = call_gex_3d + put_gex_3d
    
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
    
    df_chart_range['abs_gex_contribution'] = df_chart_range['gex'].abs()
    bucket_data = df_chart_range.groupby('strike_bucket').agg({'gex': 'sum', 'abs_gex_contribution': 'sum'})
    
    target_buckets = list(range(int(lower_bound), int(upper_bound) + 1000, 1000))
    chart_matrix = []
    
    for idx, b_strike in enumerate(target_buckets):
        gex_val = bucket_data.get('gex', {}).get(b_strike, 0.0)
        abs_gex_val = bucket_data.get('abs_gex_contribution', {}).get(b_strike, 0.0)
        chart_matrix.append({"index": idx, "strike": b_strike, "gex": gex_val, "abs_gex": abs_gex_val})

    return {
        "spot": spot_price, "call_gex": call_gex, "put_gex": put_gex, "net_gex": net_gex,
        "net_gex_3d": net_gex_3d,
        "call_weight": call_weight_pct, "max_pain": max_pain_level, "flip": flip_level,
        "breakout": breakout_price, "resistance": resistance_level, "support": support_level,
        "call_inflow": signed_call_inflow, "put_inflow": signed_put_inflow,
        "net_flow": net_flow, "cp_ratio": cp_ratio, "chart_data": chart_matrix
    }

def fmt_gex(val):
    sign = "+" if val >= 0 else "-"
    abs_val = abs(val)
    return f"{sign}{abs_val/1000:.1f}k" if abs_val >= 1000 else f"{sign}{abs_val:.1f}"

def fmt_inflow(val):
    sign = "+" if val >= 0 else ""
    abs_val = abs(val)
    return f"{sign}{val/1000:.1f}k" if abs_val >= 1000 else f"{sign}{val:.0f}"

def main(page: ft.Page):
    page.title = "Deribit GEX Terminal"
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO
    page.padding = 14

    net_axis = ft.ChartAxis(labels=[], labels_size=24)
    abs_axis = ft.ChartAxis(labels=[], labels_size=24)
    history_axis = ft.ChartAxis(labels=[], labels_size=24)

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
    cp_ratio_txt = ft.Text("0.00", size=22, weight=ft.FontWeight.BOLD, color=ft.colors.CYAN_300)

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

    history_line_chart = ft.LineChart(
        data_series=[
            ft.LineChartData(
                data_points=[],
                color=ft.colors.CYAN_400,
                stroke_width=2.5,
                curved=True,
                below_line_bgcolor=ft.colors.with_opacity(0.05, ft.colors.CYAN_400),
            )
        ],
        bottom_axis=history_axis,
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
            call_gex_txt.value = fmt_gex(m['call_gex'])
            put_gex_txt.value = fmt_gex(m['put_gex'])
            net_gex_txt.value = fmt_gex(m['net_gex'])
            net_gex_txt.color = ft.colors.GREEN_400 if m['net_gex'] >= 0 else ft.colors.RED_400
            weight_txt.value = f"{m['call_weight']:.1f}%"
            pain_txt.value = f"${m['max_pain']:,.0f}"
            flip_txt.value = f"${m['flip']:,.0f}"
            breakout_txt.value = f"${m['breakout']:,.0f}"
            res_txt.value = f"${m['resistance']:,.0f}"
            sup_txt.value = f"${m['support']:,.0f}"
            inflows_call_txt.value = fmt_inflow(m['call_inflow'])
            inflows_call_txt.color = ft.colors.GREEN_400 if m['call_inflow'] >= 0 else ft.colors.RED_400
            outflows_put_txt.value = fmt_inflow(m['put_inflow'])
            outflows_put_txt.color = ft.colors.GREEN_400 if m['put_inflow'] >= 0 else ft.colors.RED_400
            net_flow_txt.value = fmt_gex(m['net_flow'])
            net_flow_txt.color = ft.colors.GREEN_400 if m['net_flow'] >= 0 else ft.colors.RED_400
            cp_ratio_txt.value = f"{m['cp_ratio']:.2f}"
            
            try:
                snapshot = {
                    "timestamp": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
                    "gex": round(m['net_gex_3d'], 2)
                }
                redis.rpush(REDIS_KEY, json.dumps(snapshot))
                redis.ltrim(REDIS_KEY, -MAX_HISTORY_POINTS, -1)
            except Exception as ex:
                print(f"Cloud Logging Interrupted: {ex}")

            try:
                raw_records = redis.lrange(REDIS_KEY, 0, -1)
                if raw_records:
                    line_points = []
                    hist_labels = []
                    step = max(1, len(raw_records) // 6)
                    
                    for idx, record in enumerate(raw_records):
                        data = json.loads(record)
                        line_points.append(ft.LineChartDataPoint(x=idx, y=data['gex']))
                        
                        if idx % step == 0 or idx == len(raw_records) - 1:
                            hist_labels.append(
                                ft.ChartAxisLabel(
                                    value=idx,
                                    label=ft.Text(data['timestamp'], size=9, color=ft.colors.GREY_500, rotate=30)
                                )
                            )
                    history_line_chart.data_series[0].data_points = line_points
                    history_axis.labels = hist_labels
            except Exception as ex:
                print(f"Cloud Read Failure: {ex}")
            
            new_groups, abs_groups, new_labels, min_dist, spot_index = [], [], [], float('inf'), -1
            for item in m['chart_data']:
                dist = abs(item['strike'] - m['spot'])
                if dist < min_dist: min_dist, spot_index = dist, item['index']
            
            for item in m['chart_data']:
                val, abs_val, strike_val, is_spot = item['gex'], item['abs_gex'], item['strike'], (item['index'] == spot_index)
                new_groups.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=val, color=ft.colors.GREEN_400 if val >= 0 else ft.colors.RED_400, width=12, border_radius=2)]))
                abs_groups.append(ft.BarChartGroup(x=item['index'], bar_rods=[ft.BarChartRod(from_y=0, to_y=abs_val, color=ft.colors.YELLOW, width=12, border_radius=2)]))
                
                if strike_val % 2000 == 0:
                    label_color = ft.colors.BLUE_200 if is_spot else ft.colors.GREY_400
                    new_labels.append(ft.ChartAxisLabel(value=item['index'], label=ft.Text(f"{strike_val/1000:.0f}k", size=10, color=label_color, rotate=45, weight=ft.FontWeight.BOLD if is_spot else ft.FontWeight.NORMAL)))
            
            gex_bar_chart.bar_groups = new_groups
            net_axis.labels = new_labels
            
            abs_gex_chart.bar_groups = abs_groups
            abs_axis.labels = new_labels
            
            page.update()

    page.add(
        ft.Row([ft.Text("⚡ Deribit GEX Terminal", size=20, weight=ft.FontWeight.BOLD),
                ft.ElevatedButton("Refresh", on_click=refresh_dashboard, style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)))], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Card(content=ft.Container(content=ft.Row([ft.Text("BTC UNDERLYING SPOT", size=11, color=ft.colors.GREY_500), spot_txt], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), padding=12)),
        
        create_section_header("SHORT-TERM NET GEX HISTORICAL TREND (<=3D EXP)"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=25), content=history_line_chart)),
        
        create_section_header("NET GAMMA PROFILES BY STRIKE"),
        ft.Card(content=ft.Container(padding=ft.padding.only(left=5, right=15, top=15, bottom=15), content=gex_bar_chart)),
        create_section_header("ABS GEX (GROSS HEDGING ACTIVITY)"),
        ft.Card(content=ft.Container(padding=15, content=abs_gex_chart)),
        create_section_header("TOTAL GAMMA EXPOSURE"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Call Gamma", call_gex_txt), ui_row_item("Put Gamma", put_gex_txt), ui_row_item("Net Gamma", net_gex_txt), ui_row_item("Call Weight (%)", weight_txt)]))),
        create_section_header("IMPORTANT LEVELS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("Max Pain", pain_txt), ui_row_item("Flip Zone", flip_txt), ui_row_item("Breakout Price", breakout_txt), ui_row_item("Resistance Level", res_txt), ui_row_item("Support Level", sup_txt)]))),
        create_section_header("INFLOW ANALYSIS"),
        ft.Card(content=ft.Container(padding=14, content=ft.Column([ui_row_item("24h Call Inflows", inflows_call_txt), ui_row_item("24h Put Inflows", outflows_put_txt), ui_row_item("Net Volume Bias", net_flow_txt), ui_row_item("C/P Ratio", cp_ratio_txt)])))
    )
    refresh_dashboard()

if __name__ == "__main__":
    ft.app(target=main, port=int(os.environ.get("PORT", 8080)), host="0.0.0.0", view=ft.AppView.WEB_BROWSER)
