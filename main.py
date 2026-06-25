            # --- POPULATE ROLLING 24-HOUR HISTORICAL TREND ---
            try:
                raw_records = redis.lrange(REDIS_KEY, 0, -1)
                if raw_records:
                    filtered_records = []
                    time_now = datetime.now(timezone.utc)
                    
                    for record in raw_records:
                        try:
                            data = json.loads(record)
                            # Handle date wrapping properly
                            rec_time = datetime.strptime(f"{time_now.year}-{data['timestamp']}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                            if rec_time > time_now:
                                rec_time = rec_time.replace(year=time_now.year - 1)
                                
                            hours_diff = (time_now - rec_time).total_seconds() / 3600.0
                            if hours_diff <= 24.0:
                                # Keep track of actual timestamps for true sorting
                                data['epoch'] = rec_time.timestamp()
                                filtered_records.append(data)
                        except Exception:
                            continue

                    # Fallback to last 24 items if filter leaves it too empty
                    if len(filtered_records) < 2:
                        for idx, record in enumerate(raw_records[-24:]):
                            d = json.loads(record)
                            d['epoch'] = idx
                            filtered_records.append(d)

                    # CRITICAL FIX: Ensure array flows strictly from OLDEST to NEWEST
                    filtered_records.sort(key=lambda x: x['epoch'])

                    gex_in_millions = [data['gex'] / 1000000.0 for data in filtered_records]
                    max_m = max(gex_in_millions) if gex_in_millions else 50.0
                    min_m = min(gex_in_millions) if gex_in_millions else -50.0
                    
                    largest_abs = max(abs(max_m), abs(min_m), 50.0)
                    fixed_bound = math.ceil(largest_abs / 50.0) * 50.0
                    
                    history_line_chart.min_y = -fixed_bound * 1000000.0
                    history_line_chart.max_y = fixed_bound * 1000000.0

                    y_labels = []
                    current_step = -fixed_bound
                    while current_step <= fixed_bound:
                        sign = "+" if current_step > 0 else ""
                        label_text = f"{sign}{int(current_step)}M" if current_step != 0 else "0"
                        y_labels.append(
                            ft.ChartAxisLabel(
                                value=current_step * 1000000.0,
                                label=ft.Text(label_text, size=10, color=ft.colors.GREY_400)
                            )
                        )
                        current_step += 50.0
                    history_left_axis.labels = y_labels

                    line_points = []
                    total_records = len(filtered_records)
                    
                    for idx, data in enumerate(filtered_records):
                        # Map linearly over a 0 to 23 coordinate field
                        x_pos = (idx / (total_records - 1)) * 23 if total_records > 1 else idx
                        line_points.append(ft.LineChartDataPoint(x=x_pos, y=data['gex']))
                    
                    history_line_chart.data_series[0].data_points = line_points

                    # --- CHRONOLOGICAL X-AXIS LABELS (OLDEST LEFT -> NEWEST RIGHT) ---
                    x_labels = []
                    current_utc_hour = time_now.hour
                    
                    for i in range(0, 24, 3):
                        # i=0 represents 24 hours ago (Left side), i=23 represents now (Right side)
                        target_hour = (current_utc_hour - 24 + i) % 24
                        x_coord = (i / 24) * 23
                        
                        x_labels.append(
                            ft.ChartAxisLabel(
                                value=x_coord,
                                label=ft.Text(f"{target_hour:02d}:00", size=10, color=ft.colors.GREY_500, weight=ft.FontWeight.W_500)
                            )
                        )
                    history_bottom_axis.labels = x_labels

            except Exception as ex:
                print(f"Cloud Read Failure: {ex}")
