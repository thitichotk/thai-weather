from __future__ import annotations

from typing import Any, Dict, List, Tuple

import streamlit as st

# --- Bank of Thailand chart theme -------------------------------------------
BOT_FONT = "Sarabun, 'Noto Sans Thai', 'Segoe UI', system-ui, sans-serif"
_BOT_GRID = "#e3e6ea"   # grey-200 hairline
_BOT_ZERO = "#ced2d8"   # grey-300
_BOT_AXIS = "#545c66"   # grey-700
_BOT_INK = "#23282e"    # grey-900
_BOT_BRAND = "#0f2a52"  # navy-800

# Many-series colorway for station-level charts (BOT-leaning; cycles past 10).
BOT_COLORWAY = [
    "#1f7fd6", "#a97c1f", "#2a7d4f", "#c0342a", "#4a3aa7",
    "#1a9e8f", "#1c4a80", "#5bb8e8", "#6d7580", "#14345f",
]


def style_bot_fig(fig, *, colorway: bool = False):
    """Apply the BOT chart look to a cartesian Plotly figure (font, white
    surface, grey-200 hairline gridlines, grey-700 axis ink)."""
    fig.update_layout(
        font=dict(family=BOT_FONT, size=13, color=_BOT_AXIS),
        title_font=dict(family=BOT_FONT, size=16, color=_BOT_BRAND),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        legend=dict(font=dict(family=BOT_FONT, size=12, color=_BOT_INK), bgcolor="rgba(255,255,255,0)"),
        hoverlabel=dict(font=dict(family=BOT_FONT, size=12)),
        margin=dict(l=56, r=24, t=56, b=40),
    )
    # A title font without title text renders as "undefined" in Plotly 6.
    if fig.layout.title.text is None:
        fig.update_layout(title_text="")
    if colorway:
        fig.update_layout(colorway=BOT_COLORWAY)
    fig.update_xaxes(gridcolor=_BOT_GRID, zerolinecolor=_BOT_ZERO, linecolor=_BOT_GRID,
                     tickfont=dict(family=BOT_FONT, color=_BOT_AXIS), title_font=dict(family=BOT_FONT, color=_BOT_AXIS))
    fig.update_yaxes(gridcolor=_BOT_GRID, zerolinecolor=_BOT_ZERO, linecolor=_BOT_GRID,
                     tickfont=dict(family=BOT_FONT, color=_BOT_AXIS), title_font=dict(family=BOT_FONT, color=_BOT_AXIS))
    return fig


def style_bot_map(fig):
    """Lighter theme for map figures (no cartesian axes)."""
    fig.update_layout(
        font=dict(family=BOT_FONT, size=13, color=_BOT_AXIS),
        legend=dict(font=dict(family=BOT_FONT, size=12, color=_BOT_INK)),
        hoverlabel=dict(font=dict(family=BOT_FONT, size=12)),
    )
    return fig


def render_region_selector(region_colors: Dict[str, str]) -> List[str]:
    all_regions = list(region_colors.keys())
    st.markdown("เลือกภูมิภาคที่ต้องการ")

    region_cols = st.columns(3)
    region_selected_map = {}
    for idx, region in enumerate(all_regions):
        with region_cols[idx % 3]:
            swatch_col, checkbox_col = st.columns([1, 8])
            with swatch_col:
                st.markdown(
                    f"<div style='margin-top: 8px; width: 14px; height: 14px; border-radius: 3px; background-color: {region_colors[region]};'></div>",
                    unsafe_allow_html=True,
                )
            with checkbox_col:
                region_selected_map[region] = st.checkbox(
                    region,
                    value=True,
                    key=f"region_selector_{region}",
                )

    return [region for region in all_regions if region_selected_map.get(region, False)]


def _build_station_options(
    stations: Dict[str, Dict[str, Any]], selected_regions: List[str]
) -> List[Tuple[str, Dict[str, Any]]]:
    station_items = [(station_id, payload) for station_id, payload in stations.items() if payload["region"] in selected_regions]
    station_items.sort(key=lambda item: (item[1]["region"], item[1]["name"], item[0]))
    return station_items


def render_station_selector(stations: Dict[str, Dict[str, Any]], selected_regions: List[str]) -> List[str]:
    st.markdown("เลือกสถานีตรวจอากาศรายสถานี")

    if not selected_regions:
        st.info("โปรดเลือกภูมิภาคก่อน เพื่อแสดงรายชื่อสถานีที่เกี่ยวข้อง")
        return []

    station_items = _build_station_options(stations, selected_regions)
    if not station_items:
        st.warning("ไม่พบสถานีที่ตรงกับภูมิภาคที่เลือก")
        return []

    station_ids = [station_id for station_id, _ in station_items]
    station_label_map = {
        station_id: f"{payload['name']} ({station_id}) · {payload['region']}"
        for station_id, payload in station_items
    }

    select_all = st.checkbox(
        "เลือกสถานีทั้งหมดในภูมิภาคที่เลือก",
        value=True,
        key="station_selector_select_all",
    )

    if select_all:
        st.caption(f"เลือกทั้งหมด {len(station_ids)} สถานี")
        st.multiselect(
            "รายการสถานีที่เลือก",
            options=station_ids,
            default=station_ids,
            format_func=lambda station_id: station_label_map[station_id],
            disabled=True,
        )
        return station_ids

    manual_selection_key = "station_selector_manual_ids"
    previous_selection = st.session_state.get(manual_selection_key)
    if previous_selection is None:
        selection = station_ids
    else:
        selection = [station_id for station_id in previous_selection if station_id in station_ids]
        # Emptied by a region change, not by the user clearing it: start from every station again.
        if previous_selection and not selection:
            selection = station_ids
    st.session_state[manual_selection_key] = selection

    selected_station_ids = st.multiselect(
        "รายการสถานีที่เลือก",
        options=station_ids,
        format_func=lambda station_id: station_label_map[station_id],
        key=manual_selection_key,
    )

    st.caption(f"เลือกแล้ว {len(selected_station_ids)} / {len(station_ids)} สถานี")
    return selected_station_ids
