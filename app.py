import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from data_processing import build_monthly_weather_dataframe, merge_oni_labels, to_excel
from ui_components import (
    render_region_selector,
    render_station_selector,
    style_bot_fig,
    style_bot_map,
)
from weather_fetcher import REGION_COLORS, fetch_oni_data, fetch_stations_parallel, load_stations

MIN_STATION_THRESHOLD = 3
LARGE_SELECTION_HINT = 40
BASE_DIR = Path(__file__).resolve().parent

# Meteostat reads its files through urllib, which on python.org macOS builds has no CA bundle.
# Point OpenSSL at certifi's bundle instead of switching verification off.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def today_bangkok() -> date:
    return datetime.now(ZoneInfo("Asia/Bangkok")).date()


def show_error(message: str, error: Exception):
    st.error(message)
    with st.expander("รายละเอียดทางเทคนิค"):
        st.code(f"{type(error).__name__}: {error}")


def validate_date_range(start_date: date, end_date: date):
    if start_date > end_date:
        st.error("วันที่เริ่มต้นต้องไม่มากกว่าวันที่สิ้นสุด")
        return False, end_date

    today = today_bangkok()
    if start_date > today:
        st.error("วันที่เริ่มต้นต้องไม่เป็นวันในอนาคต")
        return False, end_date

    query_end_date = end_date
    if end_date > today:
        st.warning("วันที่สิ้นสุดครอบคลุมวันในอนาคต ระบบจะใช้ข้อมูลถึงวันปัจจุบันแทน")
        query_end_date = today

    return True, query_end_date


def build_station_daily_dataframe(all_weather_data, station_lookup):
    if not all_weather_data:
        return pd.DataFrame()

    df_station = pd.concat(all_weather_data, ignore_index=False)
    df_station = df_station.reset_index().rename(columns={"time": "date"})
    if "date" not in df_station.columns:
        return pd.DataFrame()

    df_station["date"] = pd.to_datetime(df_station["date"], errors="coerce")
    df_station = df_station.dropna(subset=["date", "wmo_id"])
    if df_station.empty:
        return pd.DataFrame()

    df_station["wmo_id"] = df_station["wmo_id"].astype(str)
    df_station["temp"] = pd.to_numeric(df_station.get("temp"), errors="coerce")
    df_station["prcp"] = pd.to_numeric(df_station.get("prcp"), errors="coerce")

    df_station_daily = df_station.groupby(["wmo_id", "date"], as_index=False).agg(
        temp_mean=("temp", "mean"),
        prcp_sum=("prcp", lambda x: x.sum(min_count=1)),
    )

    df_station_daily["ชื่อสถานี"] = df_station_daily["wmo_id"].apply(
        lambda station_id: station_lookup.get(station_id, {}).get("name", f"สถานี {station_id}")
    )
    df_station_daily["ภูมิภาค"] = df_station_daily["wmo_id"].apply(
        lambda station_id: station_lookup.get(station_id, {}).get("region", "ไม่ทราบภูมิภาค")
    )
    df_station_daily["station_label"] = (
        df_station_daily["ชื่อสถานี"] + " (" + df_station_daily["wmo_id"] + ")"
    )

    return df_station_daily


def monthly_table_column_config():
    """Human-readable Thai headers + units/decimals for the monthly table.

    Keys that are absent from the dataframe are ignored by Streamlit, so it is
    safe to describe every possible column here.
    """
    return {
        "year_month": st.column_config.TextColumn("เดือน"),
        "temp_mean": st.column_config.NumberColumn("อุณหภูมิเฉลี่ย (°C)", format="%.1f"),
        "tmax_max": st.column_config.NumberColumn("สูงสุดของ Tmax เฉลี่ย (°C)", format="%.1f"),
        "tmin_min": st.column_config.NumberColumn("ต่ำสุดของ Tmin เฉลี่ย (°C)", format="%.1f"),
        "rhum_mean": st.column_config.NumberColumn("ความชื้น (%)", format="%.0f"),
        "wspd_mean": st.column_config.NumberColumn("ความเร็วลม (km/h)", format="%.1f"),
        "pres_mean": st.column_config.NumberColumn("ความกดอากาศ (hPa)", format="%.1f"),
        "prcp_sum": st.column_config.NumberColumn("ปริมาณฝนรวม (mm)", format="%.1f"),
        "rainy_days": st.column_config.NumberColumn("วันฝนตก", format="%d"),
        "ONI_Index": st.column_config.NumberColumn("ดัชนี ONI", format="%.2f"),
        "ONI_Label": st.column_config.TextColumn("การจัดประเภท ONI"),
    }


# --- App setup ---
st.set_page_config(
    page_title="Thai-Weather · ระบบรวบรวมและวิเคราะห์ข้อมูลอุตุนิยมวิทยา",
    page_icon=":material/cloud:",
    layout="wide",
    menu_items={
        "about": "Thai-Weather: ระบบรวบรวมและวิเคราะห์ข้อมูลอุตุนิยมวิทยาประเทศไทย • ข้อมูลจาก Meteostat และ NOAA "
        "• โครงการนี้ไม่ใช่เว็บไซต์อย่างเป็นทางการของหน่วยงานรัฐ",
    },
)

# Load custom CSS (Bank of Thailand Design System)
style_path = BASE_DIR / ".streamlit" / "style.css"
if style_path.exists():
    css_content = style_path.read_text(encoding="utf-8")
    st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)
else:
    st.warning("ไม่พบไฟล์สไตล์ .streamlit/style.css ระบบจะใช้ธีมเริ่มต้นของ Streamlit")

# --- Header layout with Bank of Thailand styling ---
header_container = st.container()

with header_container:
    header_col1, header_col2 = st.columns([6, 1])

    with header_col1:
        st.title("ระบบรวบรวมและวิเคราะห์ข้อมูลอุตุนิยมวิทยา")
        st.header("ประเทศไทย")
        st.markdown(
            "ระบบอัตโนมัติสำหรับสืบค้นข้อมูลจากสถานีตรวจอากาศกรมอุตุนิยมวิทยาโดยใช้ Meteostat API "
            "พร้อมผนวกรวมดัชนีชี้วัดปรากฏการณ์เอลนีโญ-ลานีญา (ONI) จาก NOAA ผ่านกระบวนการทำความสะอาดข้อมูล "
            "(Data Cleaning) และแสดงผลในรูปแบบ Visualizations เพื่อสนับสนุนการตัดสินใจ"
        )

    with header_col2:
        logo_col1, logo_col2 = st.columns(2)
        with logo_col1:
            st.image(str(BASE_DIR / "assets" / "TMD.png"), width="stretch")
        with logo_col2:
            st.image(str(BASE_DIR / "assets" / "NOAA.png"), width="stretch")

st.divider()

# --- Station data loading ---
try:
    all_stations = load_stations()
except Exception as e:
    show_error("โหลดรายชื่อสถานีไม่สำเร็จ ตรวจสอบว่ามีไฟล์ stations.json ที่ถูกต้องอยู่ในโฟลเดอร์ของแอป", e)
    st.stop()

# --- User inputs ---
with st.container():
    st.markdown("### กำหนดภูมิภาคและช่วงเวลาการสืบค้นข้อมูล")
    st.caption(
        "ขั้นตอน: (1) เลือกภูมิภาค → (2) เลือกสถานี → (3) กำหนดช่วงเวลา → "
        "(4) กดปุ่มเพื่อเริ่มสืบค้นและทำความสะอาดข้อมูล"
    )
    selected_regions = render_region_selector(REGION_COLORS)
    selected_station_ids = render_station_selector(all_stations, selected_regions)

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input(
            "วันที่เริ่มต้น",
            # Five years by default: all 127 stations since 2005 was enough data to freeze the browser.
            value=date(today_bangkok().year - 5, 1, 1),
            min_value=date(1950, 1, 1),
            max_value=today_bangkok(),
        )
    with col2:
        end_date = st.date_input("วันที่สิ้นสุด", today_bangkok())

stations_in_selected_regions = {k: v for k, v in all_stations.items() if v["region"] in selected_regions}
wmo_stations = {k: v for k, v in stations_in_selected_regions.items() if k in selected_station_ids}
is_station_subset_selection = bool(stations_in_selected_regions) and (
    len(selected_station_ids) < len(stations_in_selected_regions)
)

# --- Selection summary + action ---
if wmo_stations:
    st.caption(
        f"พร้อมสืบค้นข้อมูล **{len(wmo_stations)}** สถานี จาก **{len(selected_regions)}** ภูมิภาค "
        f"ในช่วง {start_date:%d/%m/%Y} – {end_date:%d/%m/%Y}"
    )
    if len(wmo_stations) > LARGE_SELECTION_HINT:
        st.caption(
            ":material/schedule: การเลือกสถานีจำนวนมากอาจใช้เวลาสืบค้นนานขึ้น "
            "แนะนำให้จำกัดจำนวนสถานีหรือช่วงเวลาเพื่อความรวดเร็ว"
        )

# --- Execute processing ---
if st.button("ประมวลผลและทำความสะอาดข้อมูล", type="primary", width="stretch"):
    if not selected_regions:
        st.error("กรุณาเลือกข้อมูลอย่างน้อย 1 ภูมิภาค")
        st.stop()

    if not selected_station_ids:
        st.error("กรุณาเลือกสถานีอย่างน้อย 1 สถานี")
        st.stop()

    if not wmo_stations:
        st.error("ไม่พบสถานีอุตุนิยมวิทยาในภูมิภาคที่เลือก")
        st.stop()

    is_valid_date_range, query_end_date = validate_date_range(start_date, end_date)
    if not is_valid_date_range:
        st.stop()

    progress_bar = st.progress(0)
    status_text = st.empty()

    total_stations = len(wmo_stations)
    all_weather_data, fetched_stations, failed_stations, station_results = fetch_stations_parallel(
        wmo_stations, start_date, query_end_date, progress_bar, status_text
    )

    status_text.text("กำลังดำเนินการวิเคราะห์สภาพอากาศและประมวลผลดัชนี ONI...")

    success_count = len(fetched_stations)
    timeout_count = sum(1 for x in failed_stations if x["reason"] == "timeout")
    empty_count = sum(1 for x in failed_stations if x["reason"] == "empty")
    exception_count = sum(1 for x in failed_stations if x["reason"] == "exception")
    duplicate_count = sum(1 for x in failed_stations if x["reason"] == "duplicate")
    fallback_success_count = sum(1 for x in fetched_stations if x.get("source") == "fallback")

    st.info(
        f"สรุปผลการสืบค้นข้อมูลสถานี: สำเร็จ {success_count}/{total_stations} แห่ง | "
        f"เรียกข้อมูลสำรอง (Fallback) สำเร็จ {fallback_success_count} แห่ง | "
        f"ไม่มีข้อมูล {empty_count} | หมดเวลา (Timeout) {timeout_count} | ข้อผิดพลาดอื่น {exception_count}"
        + (f" | สถานีสำรองซ้ำ (ไม่นับซ้ำ) {duplicate_count}" if duplicate_count else "")
    )

    with st.expander("รายละเอียดบันทึกการสืบค้นข้อมูลรายสถานี"):
        st.dataframe(pd.DataFrame(station_results), width="stretch", hide_index=True)

    if all_weather_data:
        try:
            df_monthly = build_monthly_weather_dataframe(all_weather_data)
        except Exception as e:
            progress_bar.empty()
            status_text.empty()
            show_error("เตรียมข้อมูลสภาพอากาศไม่สำเร็จ ลองลดจำนวนสถานีหรือช่วงวันที่ แล้วกดประมวลผลอีกครั้ง", e)
            st.stop()

        try:
            df_oni = fetch_oni_data()
            df_monthly = merge_oni_labels(df_monthly, df_oni)
        except Exception as e:
            st.warning("ดึงข้อมูล ONI จาก NOAA ไม่ได้ในขณะนี้ ผลลัพธ์จึงไม่มีคอลัมน์ ONI ลองใหม่ภายหลัง")
            with st.expander("รายละเอียดทางเทคนิค"):
                st.code(f"{type(e).__name__}: {e}")

        status_text.markdown("เสร็จสิ้นกระบวนการ!")
        progress_bar.empty()

        if success_count < MIN_STATION_THRESHOLD:
            if len(selected_station_ids) < MIN_STATION_THRESHOLD and is_station_subset_selection:
                st.info(
                    "คุณเลือกวิเคราะห์แบบรายสถานีจำนวนน้อย (1-2 สถานี) "
                    "ผลลัพธ์จะแสดงลักษณะเฉพาะของสถานีที่เลือก ไม่ใช่ภาพรวมทั้งภูมิภาค"
                )
            else:
                st.warning(
                    f"สืบค้นข้อมูลได้เพียง {success_count} สถานี ซึ่งอาจไม่เพียงพอสำหรับเป็นตัวแทนของทั้งภูมิภาค"
                )

        weather_value_cols = [
            "temp_mean", "tmax_max", "tmin_min", "rhum_mean",
            "wspd_mean", "pres_mean", "prcp_sum", "rainy_days",
        ]
        incomplete_cols = [
            col for col in weather_value_cols
            if col in df_monthly.columns and df_monthly[col].isna().any()
        ]
        if incomplete_cols:
            st.success("การประมวลผลและทำความสะอาดข้อมูลเสร็จสมบูรณ์")
            st.info(
                "หมายเหตุ: ตัวชี้วัดต่อไปนี้ไม่มีข้อมูลเพียงพอในบางช่วงจึงเว้นว่างไว้: "
                + ", ".join(incomplete_cols)
            )
        else:
            st.success("การประมวลผลข้อมูลเสร็จสมบูรณ์ ข้อมูลทั้งหมดไร้ Missing Values!")

        # --- Summary metric cards ---
        month_count = df_monthly["year_month"].nunique()
        avg_temp = df_monthly["temp_mean"].mean() if "temp_mean" in df_monthly.columns else float("nan")
        avg_rain = df_monthly["prcp_sum"].mean() if "prcp_sum" in df_monthly.columns else float("nan")

        latest_phase = None
        latest_phase_month = None
        if {"ONI_Label", "ONI_Index"}.issubset(df_monthly.columns):
            labeled = df_monthly.dropna(subset=["ONI_Index"])
            if not labeled.empty:
                latest_phase = str(labeled.iloc[-1]["ONI_Label"])
                latest_phase_month = str(labeled.iloc[-1]["year_month"])

        metric_cols = st.columns(4)
        metric_cols[0].metric("สถานีที่สืบค้นสำเร็จ", f"{success_count}/{total_stations}")
        metric_cols[1].metric("จำนวนเดือนที่วิเคราะห์", f"{month_count:,}")
        metric_cols[2].metric("อุณหภูมิเฉลี่ย", f"{avg_temp:.1f} °C" if pd.notna(avg_temp) else "—")
        metric_cols[3].metric("ปริมาณฝนเฉลี่ย/เดือน", f"{avg_rain:.0f} mm" if pd.notna(avg_rain) else "—")
        if latest_phase:
            st.caption(f"สถานะ ENSO ล่าสุด ({latest_phase_month}): **{latest_phase}**")

        # --- Result tabs ---
        tab_overview, tab_station, tab_map, tab_export = st.tabs(
            [
                ":material/monitoring: ภาพรวมรายเดือน",
                ":material/sensors: รายสถานี",
                ":material/map: แผนที่สถานี",
                ":material/download: ข้อมูลและส่งออก",
            ]
        )

        with tab_overview:
            st.markdown("#### สถิติภูมิอากาศและดัชนี ONI รายเดือน")
            st.caption(
                "แต่ละแผงใช้แกนเวลาเดียวกัน (Small multiples) เลื่อนดูช่วงเวลาได้จากแถบด้านล่าง "
                "และชี้เมาส์เพื่อเทียบค่าฝน อุณหภูมิ และดัชนี ONI ในเดือนเดียวกัน"
            )
            try:
                has_oni = "ONI_Index" in df_monthly.columns

                panel_titles = ["ปริมาณฝนรวม (mm)", "อุณหภูมิเฉลี่ย (°C)"]
                if has_oni:
                    panel_titles.append("ดัชนี ONI (เอลนีโญ / ลานีญา)")
                rows = len(panel_titles)

                fig = make_subplots(
                    rows=rows,
                    cols=1,
                    shared_xaxes=True,
                    vertical_spacing=0.08,
                    subplot_titles=panel_titles,
                )

                fig.add_trace(
                    go.Scatter(
                        x=df_monthly["year_month"],
                        y=df_monthly["prcp_sum"],
                        name="ปริมาณฝน (mm)",
                        mode="lines",
                        fill="tozeroy",
                        line=dict(color="#1f7fd6", width=2),
                        fillcolor="rgba(31, 127, 214, 0.16)",
                    ),
                    row=1,
                    col=1,
                )
                fig.add_trace(
                    go.Scatter(
                        x=df_monthly["year_month"],
                        y=df_monthly["temp_mean"],
                        name="อุณหภูมิเฉลี่ย (°C)",
                        mode="lines",
                        line=dict(color="#a97c1f", width=2),
                    ),
                    row=2,
                    col=1,
                )
                if has_oni:
                    # El Niño (+0.5) / La Niña (−0.5) reference thresholds
                    fig.add_hline(y=0.5, line=dict(color="#c0342a", width=1, dash="dash"), opacity=0.4, row=3, col=1)
                    fig.add_hline(y=-0.5, line=dict(color="#2a7d4f", width=1, dash="dash"), opacity=0.4, row=3, col=1)
                    fig.add_trace(
                        go.Scatter(
                            x=df_monthly["year_month"],
                            y=df_monthly["ONI_Index"],
                            name="ดัชนี ONI",
                            mode="lines",
                            line=dict(color="#c0342a", width=2, dash="dot"),
                            customdata=df_monthly[["ONI_Label"]].values,
                            hovertemplate=(
                                "เดือน: %{x}<br>"
                                "ดัชนี ONI: %{y:.2f}<br>"
                                "ผลการจัดประเภท: %{customdata[0]}<extra></extra>"
                            ),
                        ),
                        row=3,
                        col=1,
                    )

                fig.update_layout(
                    height=560 if has_oni else 420,
                    hovermode="x unified",
                    showlegend=False,
                    title="สถิติสภาพอากาศรายเดือนเทียบดัชนี ONI",
                )
                fig.update_yaxes(title_text="mm", row=1, col=1)
                fig.update_yaxes(title_text="°C", row=2, col=1)
                if has_oni:
                    fig.update_yaxes(title_text="ONI", row=3, col=1)
                fig.update_xaxes(rangeslider=dict(visible=True, thickness=0.06), row=rows, col=1)
                style_bot_fig(fig)

                st.plotly_chart(fig, width="stretch")
            except Exception as e:
                show_error("วาดกราฟไม่สำเร็จ ตารางด้านล่างยังใช้งานได้", e)

            st.markdown("#### ตารางสรุปผลข้อมูลรายเดือน")
            st.dataframe(
                df_monthly,
                width="stretch",
                hide_index=True,
                column_config=monthly_table_column_config(),
            )

        with tab_station:
            df_station_daily = build_station_daily_dataframe(all_weather_data, wmo_stations)
            if df_station_daily.empty:
                st.warning("ไม่สามารถสร้างกราฟรายสถานีได้ เนื่องจากข้อมูลรายวันไม่เพียงพอ")
            else:
                st.caption("วิเคราะห์ข้อมูลแยกแต่ละสถานีที่เลือก (Station-Level)")
                st.markdown("#### แนวโน้มอุณหภูมิเฉลี่ยรายวันแยกตามสถานี")
                fig_station_temp = px.line(
                    df_station_daily,
                    x="date",
                    y="temp_mean",
                    color="station_label",
                    hover_name="ชื่อสถานี",
                    render_mode="webgl",
                    labels={
                        "date": "วันที่",
                        "temp_mean": "อุณหภูมิเฉลี่ย (°C)",
                        "station_label": "สถานี",
                    },
                )
                fig_station_temp.update_layout(legend_title_text="สถานี")
                style_bot_fig(fig_station_temp, colorway=True)
                st.plotly_chart(fig_station_temp, width="stretch")

                st.markdown("#### แนวโน้มปริมาณฝนรายวันแยกตามสถานี")
                fig_station_prcp = px.line(
                    df_station_daily,
                    x="date",
                    y="prcp_sum",
                    color="station_label",
                    hover_name="ชื่อสถานี",
                    render_mode="webgl",
                    labels={
                        "date": "วันที่",
                        "prcp_sum": "ปริมาณฝน (mm)",
                        "station_label": "สถานี",
                    },
                )
                fig_station_prcp.update_layout(legend_title_text="สถานี")
                style_bot_fig(fig_station_prcp, colorway=True)
                st.plotly_chart(fig_station_prcp, width="stretch")

                station_summary = (
                    df_station_daily.groupby(["station_label", "ภูมิภาค"], as_index=False)
                    .agg(
                        อุณหภูมิเฉลี่ยรวม=("temp_mean", "mean"),
                        ปริมาณฝนสะสมรวม=("prcp_sum", "sum"),
                    )
                    .sort_values(["ภูมิภาค", "station_label"])
                )
                st.markdown("#### สรุปรายสถานี")
                st.dataframe(
                    station_summary,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "station_label": st.column_config.TextColumn("สถานี"),
                        "ภูมิภาค": st.column_config.TextColumn("ภูมิภาค"),
                        "อุณหภูมิเฉลี่ยรวม": st.column_config.NumberColumn("อุณหภูมิเฉลี่ย (°C)", format="%.1f"),
                        "ปริมาณฝนสะสมรวม": st.column_config.NumberColumn("ปริมาณฝนสะสม (mm)", format="%.1f"),
                    },
                )

        with tab_map:
            st.markdown("#### แผนที่แสดงพิกัดสถานีอุตุนิยมวิทยาที่สืบค้นสำเร็จ")
            if fetched_stations:
                df_stations = pd.DataFrame(fetched_stations)
                fig_map = px.scatter_map(
                    df_stations,
                    lat="ละติจูด",
                    lon="ลองจิจูด",
                    color="ภูมิภาค",
                    category_orders={"ภูมิภาค": list(REGION_COLORS.keys())},
                    color_discrete_map=REGION_COLORS,
                    hover_name="ชื่อสถานี",
                    hover_data={
                        "ภูมิภาค": True,
                        "ที่อยู่": True,
                        "รหัสสถานี (WMO)": True,
                        "ละติจูด": True,
                        "ลองจิจูด": True,
                        "แหล่งข้อมูล": True,
                    },
                    zoom=4.5,
                    height=600,
                )
                fig_map.update_layout(
                    map_style="open-street-map",
                    margin={"r": 0, "t": 0, "l": 0, "b": 0},
                )
                style_bot_map(fig_map)
                st.plotly_chart(fig_map, width="stretch")
            else:
                st.info("ไม่มีสถานีที่สืบค้นสำเร็จสำหรับแสดงบนแผนที่")

        with tab_export:
            st.markdown("#### ส่งออกข้อมูล (รวมดัชนี ONI)")
            col_down1, col_down2, _ = st.columns([1, 1, 2])

            with col_down1:
                csv_data = df_monthly.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    label="ส่งออกเป็น CSV",
                    data=csv_data,
                    file_name=f"thailand_weather_monthly_with_oni_{start_date.year}_to_{end_date.year}.csv",
                    mime="text/csv",
                    on_click="ignore",  # a rerun here would wipe the results
                    type="primary",
                    width="stretch",
                )

            with col_down2:
                excel_data = to_excel(df_monthly)
                st.download_button(
                    label="ส่งออกเป็น Excel",
                    data=excel_data,
                    file_name=f"thailand_weather_monthly_with_oni_{start_date.year}_to_{end_date.year}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    on_click="ignore",
                    type="primary",
                    width="stretch",
                )

            st.markdown("#### พจนานุกรมข้อมูล (Data Dictionary)")
            with st.expander("คลิกเพื่อดูคำอธิบายตัวแปรในชุดข้อมูล", expanded=False):
                st.markdown(
                    """
| ชื่อตัวแปร (Field) | คำอธิบาย (Description) | หน่วย (Unit) |
| --- | --- | --- |
| **year_month** | ปีและเดือนของข้อมูล | YYYY-MM |
| **temp_mean** | อุณหภูมิเฉลี่ยประจำเดือน | องศาเซลเซียส (°C) |
| **tmax_max** | ค่าสูงสุดในเดือนของอุณหภูมิสูงสุดรายวัน (เฉลี่ยทุกสถานีที่เลือก) | องศาเซลเซียส (°C) |
| **tmin_min** | ค่าต่ำสุดในเดือนของอุณหภูมิต่ำสุดรายวัน (เฉลี่ยทุกสถานีที่เลือก) | องศาเซลเซียส (°C) |
| **rhum_mean** | ความชื้นสัมพัทธ์เฉลี่ย | เปอร์เซ็นต์ (%) |
| **wspd_mean** | ความเร็วลมเฉลี่ย | กิโลเมตรต่อชั่วโมง (km/h) |
| **pres_mean** | ความกดอากาศเฉลี่ยที่ระดับน้ำทะเล | เฮกโตปาสคาล (hPa) |
| **prcp_sum** | ปริมาณน้ำฝนสะสมรวมในเดือนนั้น (ค่าเฉลี่ยรายวันของทุกสถานีที่มีข้อมูล รวมทั้งเดือน) | มิลลิเมตร (mm) |
| **rainy_days** | จำนวนวันที่ฝนตก (ปริมาณฝน > 0.5 mm) | วัน (days) |
| **ONI_Index** | ดัชนี Oceanic Niño Index ชี้วัดความรุนแรงปรากฏการณ์เอลนีโญ/ลานีญา | - |
| **ONI_Label** | ผลการจัดประเภทความรุนแรงเอลนีโญ/ลานีญาจากค่า ONI | - |
"""
                )
    else:
        progress_bar.empty()
        status_text.empty()
        st.error("ไม่พบข้อมูลสภาพอากาศในช่วงเวลาที่ระบุ หรือล้มเหลวในการเชื่อมต่อกับทุกสถานี")
        if failed_stations:
            preview = pd.DataFrame(failed_stations)
            st.markdown("### ตัวอย่างรายละเอียดข้อผิดพลาด (10 รายการแรก)")
            st.dataframe(preview.head(10), width="stretch", hide_index=True)

st.markdown("---")
st.markdown(
    "<div class='app-footer' style='text-align: center; color: #6d7580; font-size: 0.9em;'>"
    "เว็บไซต์นี้ไม่ใช่เว็บไซต์อย่างเป็นทางการของหน่วยงานรัฐ (Disclaimer: This is not an official website)"
    "</div>",
    unsafe_allow_html=True,
)
