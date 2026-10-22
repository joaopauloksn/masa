#!/usr/bin/env python3
"""
generate_evaluation_excel.py
Generates a structured, professional Excel workbook (.xlsx) containing:
1. "Summary & KPI Dashboard" with dynamic formulas and key performance indicators.
2. "Load Test Plan & Expectation" detailing the load generator parameters, phases, devices, and theoretical msg/s vs observed msg/s.
3. "Load Test Metrics" with the full 125 rows of 15s-sampled data from the 30-minute evaluation window.
4. Native Excel Charts:
   - Chart 1: Dynamic Scaling Tracking (Delivered msg/s vs Replicas)
   - Chart 2: SLA Quality & Stability (Delivery % and P90 Latency)
   - Chart 3: Saturation & Queueing (Delivered msg/s vs Undelivered msg/s)
   - Chart 4: Resource Footprint (CPU Cores & Memory MiB)
"""

import os
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, Reference, BarChart

def create_excel_with_charts(csv_path: str, output_path: str, system_name: str = "MASA Autoscaler"):
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['time_str'] = df['timestamp'].dt.strftime('%H:%M:%S')

    wb = openpyxl.Workbook()
    
    # -------------------------------------------------------------
    # SHEET 1: Summary & Charts Dashboard
    # -------------------------------------------------------------
    ws_dash = wb.active
    ws_dash.title = "Summary & KPI Dashboard"
    ws_dash.views.sheetView[0].showGridLines = True

    # -------------------------------------------------------------
    # SHEET 2: Load Test Plan & Expected Workload
    # -------------------------------------------------------------
    ws_plan = wb.create_sheet(title="Load Test Plan")
    ws_plan.views.sheetView[0].showGridLines = True

    # -------------------------------------------------------------
    # SHEET 3: Raw Metrics Data
    # -------------------------------------------------------------
    ws_data = wb.create_sheet(title="Load Test Metrics")
    ws_data.views.sheetView[0].showGridLines = True

    # Styles
    title_font = Font(name="Calibri", size=16, bold=True, color="1F497D")
    section_font = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
    section_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    sub_fill = PatternFill(start_color="2F5597", end_color="2F5597", fill_type="solid")
    table_header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    table_header_font = Font(name="Calibri", size=11, bold=True, color="002060")
    kpi_name_font = Font(name="Calibri", size=10, bold=True, color="333333")
    kpi_val_font = Font(name="Calibri", size=11, bold=True, color="002060")
    bold_font = Font(name="Calibri", size=11, bold=True)
    regular_font = Font(name="Calibri", size=11)
    center_align = Alignment(horizontal="center", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")
    
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )
    thick_bottom_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='medium', color='1F497D')
    )

    # =============================================================
    # POPULATE SHEET 3: Load Test Metrics (Raw Data)
    # =============================================================
    # Pre-calculate timeline schedule mapping to assign planned expected throughput per sample row
    steps_schedule = [
        (1.25, 1, 500, 1000.0),
        (1.25, 2, 1000, 2000.0),
        (1.25, 4, 2000, 4000.0),
        (1.25, 6, 3000, 6000.0),
        (5.00, 10, 5000, 10000.0),
        (5.00/3, 6, 3000, 6000.0),
        (5.00/3, 4, 2000, 4000.0),
        (5.00/3, 2, 1000, 2000.0),
        (5.00, 10, 5000, 10000.0),
        (5.00, 12, 6000, 12000.0),
        (1.25, 8, 4000, 8000.0),
        (1.25, 6, 3000, 6000.0),
        (1.25, 4, 2000, 4000.0),
        (1.25, 2, 1000, 2000.0),
    ]

    def get_expected_for_timestamp(idx_row, total_rows):
        elapsed_min = (idx_row * 15.0) / 60.0
        cur_min = 0.0
        for dur, pods, clients, exp_tput in steps_schedule:
            cur_min += dur
            if elapsed_min <= cur_min:
                return exp_tput, clients
        return steps_schedule[-1][3], steps_schedule[-1][2]

    headers = [
        "Time (UTC)",
        "Replicas",
        "Delivered (msg/s)",
        "Expected (msg/s)",
        "Undelivered (msg/s)",
        "Delivery Rate (%)",
        "P90 Latency (s)",
        "CPU Cores",
        "Memory (MiB)",
        "Working Set Memory (MiB)",
        "Full Timestamp"
    ]
    ws_data.append(headers)

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    for col_idx in range(1, len(headers) + 1):
        cell = ws_data.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = section_fill
        cell.alignment = center_align

    for idx_r, (_, row) in enumerate(df.iterrows()):
        exp_msgs, _ = get_expected_for_timestamp(idx_r, len(df))
        ws_data.append([
            row['time_str'],
            int(row['replicas']),
            round(float(row['delivered_rate']), 2),
            round(exp_msgs, 1),
            round(float(row['undelivered_rate']), 2),
            round(float(row['delivery_pct']), 2),
            round(float(row['p90_latency_s']), 4),
            round(float(row['cpu_cores']), 3),
            round(float(row['memory_mib']), 2),
            round(float(row['memory_working_set_mib']), 2),
            str(row['timestamp'])
        ])

    num_rows = len(df) + 1

    for r in range(2, num_rows + 1):
        ws_data.cell(row=r, column=1).alignment = center_align
        ws_data.cell(row=r, column=2).number_format = '#,##0'
        ws_data.cell(row=r, column=3).number_format = '#,##0.0'
        ws_data.cell(row=r, column=4).number_format = '#,##0.0'
        ws_data.cell(row=r, column=5).number_format = '#,##0.0'
        ws_data.cell(row=r, column=6).number_format = '0.00"%"'
        ws_data.cell(row=r, column=7).number_format = '0.0000'
        ws_data.cell(row=r, column=8).number_format = '0.000'
        ws_data.cell(row=r, column=9).number_format = '#,##0.0'
        ws_data.cell(row=r, column=10).number_format = '#,##0.0'

    for col in ws_data.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws_data.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # =============================================================
    # POPULATE SHEET 2: Load Test Plan & Expected Workload
    # =============================================================
    ws_plan['A1'] = "MQTT Load Testing Plan: Configuration & Expected Traffic"
    ws_plan['A1'].font = title_font

    ws_plan['A2'] = "Workload profile, client/device counts, message frequency, and expected throughput per phase."
    ws_plan['A2'].font = Font(name="Calibri", size=10, italic=True, color="595959")

    # Section 1: Pod Level Configuration
    ws_plan.merge_cells('A4:D4')
    ws_plan['A4'] = "1. Load Generator Pod Configuration (Per-Pod Parameters)"
    ws_plan['A4'].font = section_font
    ws_plan['A4'].fill = section_fill
    ws_plan['A4'].alignment = left_align

    pod_params = [
        ("Parameter", "Environment Variable", "Configured Value", "Description / Impact"),
        ("Devices / Clients per Pod", "NUM_CLIENTS", 500, "Simulated concurrent IoT devices per load tester pod"),
        ("Publish Interval (s)", "MQTT_MESSAGE_FREQUENCY", 0.5, "Message interval per client (0.5s = 2 msgs/sec per client)"),
        ("Message Rate per Client", "Formula: 1 / Interval", 2.0, "Messages generated per second per simulated device"),
        ("Expected Throughput per Pod", "Formula: Clients * (1/Interval)", 1000.0, "Theoretical maximum message output per load-tester pod (msg/s)"),
        ("Quality of Service (QoS)", "USE_QOS", 2, "MQTT QoS 2 (Exactly-once delivery with 4-step handshake)"),
        ("Max In-Flight Messages", "MAX_INFLIGHT", 20, "Concurrent unacknowledged QoS 2 packets allowed per client"),
        ("Payload Size Multiplier", "PAYLOAD_MULTIPLIER", 1, "Simulates standard multi-sensor JSON room telemetry"),
        ("Target Broker Deployment", "DEPLOYMENT_NAME", "iotoccupancy-sample-mosquitto", "Mosquitto MQTT broker in namespace 'iotoccupancy'"),
    ]

    for r_idx, row_vals in enumerate(pod_params, start=5):
        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws_plan.cell(row=r_idx, column=c_idx, value=val)
            cell.border = thin_border
            if r_idx == 5:
                cell.fill = table_header_fill
                cell.font = table_header_font
                cell.alignment = center_align
            else:
                cell.font = regular_font
                if c_idx == 3 and isinstance(val, (int, float)):
                    cell.alignment = right_align
                    cell.number_format = '#,##0.0' if isinstance(val, float) else '#,##0'
                else:
                    cell.alignment = left_align

    # Section 2: Phase Breakdown & Expectations Table
    start_phase_row = 16
    ws_plan.merge_cells(f'A{start_phase_row}:H{start_phase_row}')
    ws_plan[f'A{start_phase_row}'] = "2. Load Test Phases: Expected Clients & Message Throughput vs Actual Performance"
    ws_plan[f'A{start_phase_row}'].font = section_font
    ws_plan[f'A{start_phase_row}'].fill = section_fill
    ws_plan[f'A{start_phase_row}'].alignment = left_align

    phase_headers = [
        "Phase Name",
        "Step / Action",
        "Duration (min)",
        "Load Pods",
        "Simulated Devices (Clients)",
        "Expected Generation (msg/s)",
        "Avg Observed Delivered (msg/s)",
        "Generation Efficiency (%)",
        "Observed Mosquitto Replicas",
        "Operational Observations"
    ]

    for c_idx, h_text in enumerate(phase_headers, start=1):
        cell = ws_plan.cell(row=start_phase_row + 1, column=c_idx, value=h_text)
        cell.fill = table_header_fill
        cell.font = table_header_font
        cell.alignment = center_align
        cell.border = thin_border

    # Sample ranges for each step in Sheet 3 ('Load Test Metrics')
    # 125 data points sampled every 15s (4 samples/min) across 30 mins:
    # Step 1: 0.0 - 1.25m (rows 2:7)
    # Step 2: 1.25 - 2.50m (rows 7:12)
    # Step 3: 2.50 - 3.75m (rows 12:17)
    # Step 4: 3.75 - 5.00m (rows 17:22)
    # Step 5: 5.00 - 10.00m (rows 22:42)
    # Step 6: 10.00 - 11.67m (rows 42:49)
    # Step 7: 11.67 - 13.33m (rows 49:55)
    # Step 8: 13.33 - 15.00m (rows 55:62)
    # Step 9: 15.00 - 20.00m (rows 62:82)
    # Step 10: 20.00 - 25.00m (rows 82:102)
    # Step 11: 25.00 - 26.25m (rows 102:107)
    # Step 12: 26.25 - 27.50m (rows 107:112)
    # Step 13: 27.50 - 28.75m (rows 112:117)
    # Step 14: 28.75 - 30.00m (rows 117:126)

    phase_data = [
        ("Phase 1: Ramp-Up", "Step 1: Baseline Load", 1.25, 1, "=D18*C6", "=E18*C8", "=AVERAGE('Load Test Metrics'!C2:C7)", "=G18/F18", "1 - 7", "Initial scale calibration, zero message drop (P90: 0.45s)"),
        ("Phase 1: Ramp-Up", "Step 2: Moderate Load", 1.25, 2, "=D19*C6", "=E19*C8", "=AVERAGE('Load Test Metrics'!C7:C12)", "=G19/F19", "3 - 4", "Scaled smoothly to 3-4 pods (P90: 0.37s)"),
        ("Phase 1: Ramp-Up", "Step 3: Medium Load", 1.25, 4, "=D20*C6", "=E20*C8", "=AVERAGE('Load Test Metrics'!C12:C17)", "=G20/F20", "3 - 4", "Stable throughput delivery; scaled to 4 pods"),
        ("Phase 1: Ramp-Up", "Step 4: High Load", 1.25, 6, "=D21*C6", "=E21*C8", "=AVERAGE('Load Test Metrics'!C17:C22)", "=G21/F21", "4 - 6", "Scaled to 6 pods with sub-400ms latency"),
        ("Phase 2: Spike 1", "Sustained Peak 1", 5.00, 10, "=D22*C6", "=E22*C8", "=AVERAGE('Load Test Metrics'!C22:C42)", "=G22/F22", "8 - 12", "Peaked at 5,414 msg/s, dynamically allocated 12 pods"),
        ("Phase 3: Cool-Down 1", "Step 1: Step Down High", 1.67, 6, "=D23*C6", "=E23*C8", "=AVERAGE('Load Test Metrics'!C42:C49)", "=G23/F23", "10 - 12", "Autoscaler initiated gradual cool-down"),
        ("Phase 3: Cool-Down 1", "Step 2: Step Down Medium", 1.67, 4, "=D24*C6", "=E24*C8", "=AVERAGE('Load Test Metrics'!C49:C55)", "=G24/F24", "8 - 10", "Scaled down to 8 pods, maintaining 99.8% delivery"),
        ("Phase 3: Cool-Down 1", "Step 3: Step Down Low", 1.67, 2, "=D25*C6", "=E25*C8", "=AVERAGE('Load Test Metrics'!C55:C62)", "=G25/F25", "5 - 6", "Stabilized at 5 pods (~1,800 msg/s delivered)"),
        ("Phase 4: Spike 2 (Heavy)", "Step 1: Flash Spike (10 Pods)", 5.00, 10, "=D26*C6", "=E26*C8", "=AVERAGE('Load Test Metrics'!C62:C82)", "=G26/F26", "5 - 12", "Flash crowd spike; peak burst reached 9,918 msg/s"),
        ("Phase 4: Spike 2 (Heavy)", "Step 2: Absolute Peak (12 Pods)", 5.00, 12, "=D27*C6", "=E27*C8", "=AVERAGE('Load Test Metrics'!C82:C102)", "=G27/F27", "12 - 15", "Scaled to 14-15 pods, sustained ~6,800 msg/s at 99.9%"),
        ("Phase 5: Cool-Down 2", "Step 1: Step Down 8 Pods", 1.25, 8, "=D28*C6", "=E28*C8", "=AVERAGE('Load Test Metrics'!C102:C107)", "=G28/F28", "13 - 14", "Scaled down to 14 pods, throughput ~6,100 msg/s"),
        ("Phase 5: Cool-Down 2", "Step 2: Step Down 6 Pods", 1.25, 6, "=D29*C6", "=E29*C8", "=AVERAGE('Load Test Metrics'!C107:C112)", "=G29/F29", "12 - 13", "Scaled down to 12-13 pods, throughput ~4,200 msg/s"),
        ("Phase 5: Cool-Down 2", "Step 3: Step Down 4 Pods", 1.25, 4, "=D30*C6", "=E30*C8", "=AVERAGE('Load Test Metrics'!C112:C117)", "=G30/F30", "8 - 10", "Scaled down to 8-10 pods, throughput ~3,050 msg/s"),
        ("Phase 5: Cool-Down 2", "Step 4: Baseline (2 Pods)", 1.25, 2, "=D31*C6", "=E31*C8", "=AVERAGE('Load Test Metrics'!C117:C126)", "=G31/F31", "6 - 10", "Scaled down toward baseline; load test completed"),
    ]

    for r_offset, r_data in enumerate(phase_data, start=start_phase_row + 2):
        for c_idx, val in enumerate(r_data, start=1):
            cell = ws_plan.cell(row=r_offset, column=c_idx, value=val)
            cell.border = thin_border
            cell.font = regular_font
            if c_idx in [3, 4, 5, 6, 7, 8]:
                cell.alignment = right_align
                if c_idx == 3:
                    cell.number_format = '0.00'
                elif c_idx in [4, 5]:
                    cell.number_format = '#,##0'
                elif c_idx in [6, 7]:
                    cell.number_format = '#,##0.0'
                elif c_idx == 8:
                    cell.number_format = '0.0%'
            elif c_idx in [1, 2, 9]:
                cell.alignment = center_align
            else:
                cell.alignment = left_align

    # Total row
    tot_row = start_phase_row + len(phase_data) + 2
    ws_plan.cell(row=tot_row, column=1, value="Total Test Window").font = bold_font
    ws_plan.cell(row=tot_row, column=1).alignment = left_align
    ws_plan.cell(row=tot_row, column=1).border = thick_bottom_border
    
    ws_plan.cell(row=tot_row, column=2, value="14 Test Steps Across 5 Phases").font = bold_font
    ws_plan.cell(row=tot_row, column=2).alignment = left_align
    ws_plan.cell(row=tot_row, column=2).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=3, value=f"=SUM(C18:C{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=3).alignment = right_align
    ws_plan.cell(row=tot_row, column=3).number_format = '0.0" min"'
    ws_plan.cell(row=tot_row, column=3).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=4, value=f"=MAX(D18:D{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=4).alignment = right_align
    ws_plan.cell(row=tot_row, column=4).number_format = '#,##0" pods max"'
    ws_plan.cell(row=tot_row, column=4).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=5, value=f"=MAX(E18:E{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=5).alignment = right_align
    ws_plan.cell(row=tot_row, column=5).number_format = '#,##0" devices max"'
    ws_plan.cell(row=tot_row, column=5).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=6, value=f"=MAX(F18:F{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=6).alignment = right_align
    ws_plan.cell(row=tot_row, column=6).number_format = '#,##0" msg/s peak"'
    ws_plan.cell(row=tot_row, column=6).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=7, value=f"=AVERAGE(G18:G{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=7).alignment = right_align
    ws_plan.cell(row=tot_row, column=7).number_format = '#,##0.0" msg/s avg"'
    ws_plan.cell(row=tot_row, column=7).border = thick_bottom_border

    ws_plan.cell(row=tot_row, column=8, value=f"=AVERAGE(H18:H{tot_row-1})").font = bold_font
    ws_plan.cell(row=tot_row, column=8).alignment = right_align
    ws_plan.cell(row=tot_row, column=8).number_format = '0.0%'
    ws_plan.cell(row=tot_row, column=8).border = thick_bottom_border

    for c_idx in [9, 10]:
        cell = ws_plan.cell(row=tot_row, column=c_idx, value="Complete End-to-End Scalability Evaluated" if c_idx==10 else "1 - 15")
        cell.font = bold_font
        cell.alignment = left_align if c_idx==10 else center_align
        cell.border = thick_bottom_border

    for col in ws_plan.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws_plan.column_dimensions[col_letter].width = max(max_len + 3, 15)
    
    ws_plan.column_dimensions['A'].width = 24
    ws_plan.column_dimensions['B'].width = 30
    ws_plan.column_dimensions['E'].width = 26
    ws_plan.column_dimensions['F'].width = 24
    ws_plan.column_dimensions['G'].width = 26
    ws_plan.column_dimensions['H'].width = 22
    ws_plan.column_dimensions['I'].width = 26
    ws_plan.column_dimensions['J'].width = 45

    # -------------------------------------------------------------
    # Add Native Chart to Load Test Plan tab:
    # Chart A: Expected Clients (Devices) and Message Generation across steps (Original)
    # -------------------------------------------------------------
    plan_cats = Reference(ws_plan, min_col=2, min_row=18, max_row=tot_row-1)

    chart_plan = BarChart()
    chart_plan.title = "1. Planned Workload: Simulated Clients (Devices) & Message Rate per Step"
    chart_plan.style = 10
    chart_plan.y_axis.title = "Simulated Clients / Devices"
    chart_plan.x_axis.title = "Test Step / Phase Action"
    chart_plan.width = 19
    chart_plan.height = 11

    # Column E: Simulated Devices (Clients)
    data_clients = Reference(ws_plan, min_col=5, min_row=17, max_row=tot_row-1)
    chart_plan.add_data(data_clients, titles_from_data=True)
    chart_plan.set_categories(plan_cats)

    # Column F: Expected Generation (msg/s) on secondary axis
    chart_plan_sec = LineChart()
    data_msgs = Reference(ws_plan, min_col=6, min_row=17, max_row=tot_row-1)
    chart_plan_sec.add_data(data_msgs, titles_from_data=True)
    chart_plan_sec.y_axis.title = "Expected Throughput (msg/s)"
    chart_plan_sec.y_axis.axId = 300
    chart_plan_sec.y_axis.crosses = "max"
    chart_plan += chart_plan_sec

    ws_plan.add_chart(chart_plan, "A35")

    # -------------------------------------------------------------
    # Chart B: Step-by-Step Comparison: Expected vs Observed Delivered msg/s (New)
    # -------------------------------------------------------------
    chart_comp = LineChart()
    chart_comp.title = "2. Step Performance: Expected Generation vs Observed Delivered Throughput (msg/s)"
    chart_comp.style = 13
    chart_comp.y_axis.title = "Message Throughput (msg/s)"
    chart_comp.x_axis.title = "Test Step / Phase Action"
    chart_comp.width = 19
    chart_comp.height = 11

    # Column F & G: Expected (msg/s) and Avg Observed Delivered (msg/s)
    data_exp_obs = Reference(ws_plan, min_col=6, max_col=7, min_row=17, max_row=tot_row-1)
    chart_comp.add_data(data_exp_obs, titles_from_data=True)
    chart_comp.set_categories(plan_cats)

    ws_plan.add_chart(chart_comp, "F35")

    # =============================================================
    # POPULATE SHEET 1: Summary Dashboard & KPIs
    # =============================================================
    ws_dash['A1'] = f"{system_name}: 30-Minute Full Load Test Performance Evaluation"
    ws_dash['A1'].font = title_font

    ws_dash['A2'] = f"Evaluation Window: {df['time_str'].iloc[0]} - {df['time_str'].iloc[-1]} UTC | Microservice: iotoccupancy-sample-mosquitto | Total Samples: {len(df)}"
    ws_dash['A2'].font = Font(name="Calibri", size=10, italic=True, color="595959")

    # KPI Summary Header
    ws_dash.merge_cells('A4:B4')
    ws_dash['A4'] = "Key Performance Indicators (KPIs)"
    ws_dash['A4'].font = section_font
    ws_dash['A4'].fill = section_fill
    ws_dash['A4'].alignment = center_align

    ws_dash.merge_cells('C4:D4')
    ws_dash['C4'] = "SLA Compliance & Reliability"
    ws_dash['C4'].font = section_font
    ws_dash['C4'].fill = section_fill
    ws_dash['C4'].alignment = center_align

    kpi_rows = [
        ("Total Duration (min)", "=ROUND((ROW('Load Test Metrics'!A126)-2)*15/60, 1)", "SLA Target Latency (P90)", 1.0, "0.0", "0.0\"s\""),
        ("Min Replicas", f"=MIN('Load Test Metrics'!B2:B{num_rows})", "P90 Compliance (<= 1.0s)", f'=COUNTIF(\'Load Test Metrics\'!G2:G{num_rows}, "<=1.0")/{len(df)}', "0", "0.0%"),
        ("Max Replicas", f"=MAX('Load Test Metrics'!B2:B{num_rows})", "Sub-500ms P90 (<= 0.5s)", f'=COUNTIF(\'Load Test Metrics\'!G2:G{num_rows}, "<=0.5")/{len(df)}', "0", "0.0%"),
        ("Average Replicas", f"=AVERAGE('Load Test Metrics'!B2:B{num_rows})", "SLA Target Delivery Rate", 0.95, "0.0", "0.0%"),
        ("Peak Throughput (msg/s)", f"=MAX('Load Test Metrics'!C2:C{num_rows})", "Delivery Rate Compliance (>=95%)", f'=COUNTIF(\'Load Test Metrics\'!F2:F{num_rows}, ">=95.0")/{len(df)}', "#,##0.0", "0.0%"),
        ("Avg Throughput (msg/s)", f"=AVERAGE('Load Test Metrics'!C2:C{num_rows})", "Delivery Rate Compliance (>=99%)", f'=COUNTIF(\'Load Test Metrics\'!F2:F{num_rows}, ">=99.0")/{len(df)}', "#,##0.0", "0.0%"),
        ("Peak CPU Cores", f"=MAX('Load Test Metrics'!H2:H{num_rows})", "Overall Mean Delivery Rate", f"=AVERAGE('Load Test Metrics'!F2:F{num_rows})/100", "0.00", "0.00%"),
        ("Avg CPU Cores", f"=AVERAGE('Load Test Metrics'!H2:H{num_rows})", "Mean P90 Latency", f"=AVERAGE('Load Test Metrics'!G2:G{num_rows})", "0.00", "0.0000\"s\""),
        ("Peak Memory (MiB)", f"=MAX('Load Test Metrics'!I2:I{num_rows})", "Min Delivery Rate", f"=MIN('Load Test Metrics'!F2:F{num_rows})/100", "#,##0.0", "0.00%"),
        ("Avg Memory (MiB)", f"=AVERAGE('Load Test Metrics'!I2:I{num_rows})", "Max P90 Latency", f"=MAX('Load Test Metrics'!G2:G{num_rows})", "#,##0.0", "0.0000\"s\""),
    ]

    for idx, (label_ab, formula_b, label_cd, formula_d, fmt_b, fmt_d) in enumerate(kpi_rows, start=5):
        c_a = ws_dash.cell(row=idx, column=1, value=label_ab)
        c_b = ws_dash.cell(row=idx, column=2, value=formula_b)
        c_a.font = kpi_name_font
        c_b.font = kpi_val_font
        c_a.border = thin_border
        c_b.border = thin_border
        c_b.alignment = Alignment(horizontal="right")
        c_b.number_format = fmt_b
        
        c_c = ws_dash.cell(row=idx, column=3, value=label_cd)
        c_d = ws_dash.cell(row=idx, column=4, value=formula_d)
        c_c.font = kpi_name_font
        c_d.font = kpi_val_font
        c_c.border = thin_border
        c_d.border = thin_border
        c_d.alignment = Alignment(horizontal="right")
        c_d.number_format = fmt_d

    ws_dash.column_dimensions['A'].width = 24
    ws_dash.column_dimensions['B'].width = 16
    ws_dash.column_dimensions['C'].width = 32
    ws_dash.column_dimensions['D'].width = 16

    # =============================================================
    # CREATE EXCEL CHARTS
    # =============================================================
    cats = Reference(ws_data, min_col=1, min_row=2, max_row=num_rows)

    # CHART 1: Dynamic Scaling Tracking (Throughput vs Replicas)
    chart1 = LineChart()
    chart1.title = "1. Autoscaling Tracking: Delivered Throughput vs Replicas"
    chart1.style = 13
    chart1.y_axis.title = "Throughput (msg/s)"
    chart1.x_axis.title = "Time (UTC)"
    chart1.width = 19
    chart1.height = 11

    data_tput = Reference(ws_data, min_col=3, min_row=1, max_row=num_rows)
    chart1.add_data(data_tput, titles_from_data=True)
    chart1.set_categories(cats)

    chart1_sec = LineChart()
    data_rep = Reference(ws_data, min_col=2, min_row=1, max_row=num_rows)
    chart1_sec.add_data(data_rep, titles_from_data=True)
    chart1_sec.y_axis.title = "Pod Replicas"
    chart1_sec.y_axis.axId = 200
    chart1_sec.y_axis.crosses = "max"
    chart1 += chart1_sec

    ws_dash.add_chart(chart1, "F4")

    # CHART 2: SLA Quality (Delivery % & P90 Latency)
    chart2 = LineChart()
    chart2.title = "2. SLA Quality & Stability: Delivery Rate (%) vs P90 Latency (s)"
    chart2.style = 13
    chart2.y_axis.title = "Delivery Rate (%)"
    chart2.x_axis.title = "Time (UTC)"
    chart2.width = 19
    chart2.height = 11

    # Col 6: Delivery Rate (%)
    data_pct = Reference(ws_data, min_col=6, min_row=1, max_row=num_rows)
    chart2.add_data(data_pct, titles_from_data=True)
    chart2.set_categories(cats)

    # Col 7: P90 Latency (s) (secondary axis)
    chart2_sec = LineChart()
    data_lat = Reference(ws_data, min_col=7, min_row=1, max_row=num_rows)
    chart2_sec.add_data(data_lat, titles_from_data=True)
    chart2_sec.y_axis.title = "P90 Latency (seconds)"
    chart2_sec.y_axis.axId = 201
    chart2_sec.y_axis.crosses = "max"
    chart2 += chart2_sec

    ws_dash.add_chart(chart2, "F27")

    # CHART 3: Saturation & Queueing (Delivered vs Undelivered msg/s)
    chart3 = LineChart()
    chart3.title = "3. Saturation & Queueing: Delivered vs Undelivered Throughput"
    chart3.style = 13
    chart3.y_axis.title = "Message Rate (msg/s)"
    chart3.x_axis.title = "Time (UTC)"
    chart3.width = 19
    chart3.height = 11

    # Col 3: Delivered (msg/s), Col 5: Undelivered (msg/s)
    data_deliv = Reference(ws_data, min_col=3, min_row=1, max_row=num_rows)
    data_undeliv = Reference(ws_data, min_col=5, min_row=1, max_row=num_rows)
    chart3.add_data(data_deliv, titles_from_data=True)
    chart3.add_data(data_undeliv, titles_from_data=True)
    chart3.set_categories(cats)

    ws_dash.add_chart(chart3, "A16")

    # CHART 4: Cluster Resource Utilization (CPU & Memory)
    chart4 = LineChart()
    chart4.title = "4. Resource Footprint: Aggregate CPU Cores vs Memory (MiB)"
    chart4.style = 13
    chart4.y_axis.title = "CPU Cores"
    chart4.x_axis.title = "Time (UTC)"
    chart4.width = 19
    chart4.height = 11

    # Col 8: CPU Cores
    data_cpu = Reference(ws_data, min_col=8, min_row=1, max_row=num_rows)
    chart4.add_data(data_cpu, titles_from_data=True)
    chart4.set_categories(cats)

    # Col 9: Memory (MiB) (secondary axis)
    chart4_sec = LineChart()
    data_mem = Reference(ws_data, min_col=9, min_row=1, max_row=num_rows)
    chart4_sec.add_data(data_mem, titles_from_data=True)
    chart4_sec.y_axis.title = "Memory (MiB)"
    chart4_sec.y_axis.axId = 202
    chart4_sec.y_axis.crosses = "max"
    chart4 += chart4_sec

    ws_dash.add_chart(chart4, "A39")

    # CHART 5: Continuous Timeline Tracking: Expected Generation vs Actual Delivered Throughput (msg/s)
    chart5 = LineChart()
    chart5.title = "5. Continuous Throughput Tracking: Expected Generation vs Actual Delivered (msg/s)"
    chart5.style = 13
    chart5.y_axis.title = "Message Throughput (msg/s)"
    chart5.x_axis.title = "Time (UTC)"
    chart5.width = 19
    chart5.height = 11

    # Col 3 & 4: Delivered (msg/s) and Expected (msg/s) across all 125 continuous time points
    data_tput_comp = Reference(ws_data, min_col=3, max_col=4, min_row=1, max_row=num_rows)
    chart5.add_data(data_tput_comp, titles_from_data=True)
    chart5.set_categories(cats)

    ws_dash.add_chart(chart5, "F50")

    # Save workbook
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    wb.save(output_path)
    print(f"Successfully generated Excel workbook with charts and load test plan at: {output_path}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Load Test Evaluation Excel Report")
    parser.add_argument("--csv", type=str, default="ginga/masa-i/masa_load_test_30min_sep_25.csv", help="Path to input CSV")
    parser.add_argument("--output", type=str, default="ginga/masa-i/masa_load_test_evaluation_report.xlsx", help="Path to output Excel file")
    parser.add_argument("--system-name", type=str, default=None, help="System name for report header (e.g. 'MASA Autoscaler' or 'KEDA + HPA Baseline')")
    args = parser.parse_args()

    sys_name = args.system_name
    if not sys_name:
        if "hpa" in args.csv.lower() or "hpa" in args.output.lower():
            sys_name = "KEDA + HPA Baseline Autoscaler"
        else:
            sys_name = "MASA Autoscaler"

    create_excel_with_charts(args.csv, args.output, sys_name)
