from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict

import meteostat as ms
import pandas as pd
import requests
import streamlit as st

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
MAX_PARALLEL_WORKERS = 12
ONI_SOURCE_URL = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/oni/v6/"

# Anchor the station master file to this module's directory so the app works
# regardless of the current working directory it is launched from.
STATIONS_FILE = Path(__file__).resolve().parent / "stations.json"

# Bank of Thailand-harmonized categorical palette. Validated with the dataviz
# skill (Machado-2009 CVD, --pairs all on a white surface): worst pair ΔE 12.3,
# above the ≥12 target. Colour is a secondary cue here — the map/legend/hover
# also carry the region name.
REGION_COLORS = {
    "ภาคเหนือ": "#1f7fd6",              # blue-500
    "ภาคตะวันออกเฉียงเหนือ": "#a97c1f",  # gold-600
    "ภาคตะวันตก": "#2a7d4f",            # green-600
    "ภาคกลาง": "#c0342a",              # red-600
    "ภาคตะวันออก": "#4a3aa7",           # violet
    "ภาคใต้": "#1a9e8f",               # teal
}


@st.cache_data(show_spinner=False)
def load_stations() -> Dict[str, Dict[str, Any]]:
    if not STATIONS_FILE.exists():
        raise FileNotFoundError("ไม่พบไฟล์ stations.json กรุณาแปลงไฟล์สถานีเป็น JSON ก่อนใช้งาน")

    with open(STATIONS_FILE, "r", encoding="utf-8") as f:
        stations = json.load(f)

    if not isinstance(stations, dict):
        raise ValueError("โครงสร้างไฟล์ stations.json ไม่ถูกต้อง")

    cleaned_stations: Dict[str, Dict[str, Any]] = {}
    for station_id, payload in stations.items():
        if not isinstance(payload, dict):
            continue

        required_keys = {"name", "address", "lat", "lon", "region"}
        if not required_keys.issubset(payload.keys()):
            continue

        try:
            cleaned_stations[str(station_id)] = {
                "name": str(payload["name"]).strip(),
                "address": str(payload["address"]).strip(),
                "lat": float(payload["lat"]),
                "lon": float(payload["lon"]),
                "region": str(payload["region"]).strip(),
            }
        except (TypeError, ValueError):
            continue

    if not cleaned_stations:
        raise ValueError("ไฟล์ stations.json ไม่มีข้อมูลสถานีที่ใช้งานได้")

    return cleaned_stations


class EmptyStationData(Exception):
    """Meteostat had no rows for the station and range."""


# Raising on empty keeps empty answers out of the cache, so a later run asks Meteostat again.
@st.cache_data(ttl=86400, max_entries=1024, show_spinner=False)
def fetch_daily_dataframe(station_id: str, start, end) -> pd.DataFrame:
    df = ms.daily(station_id, start, end).fetch()
    if df is None or df.empty:
        raise EmptyStationData(station_id)
    return df


@st.cache_data(ttl=86400, max_entries=1024, show_spinner=False)
def find_fallback_station(station_id: str, lat: float, lon: float):
    """Nearest Meteostat station within 50 km that is not the station itself."""
    nearby = ms.stations.nearby(ms.Point(lat, lon), limit=5)
    for nearby_id, row in nearby.iterrows():
        if str(nearby_id) != station_id:
            return {
                "id": str(nearby_id),
                "name": str(row["name"]),
                "lat": float(row["latitude"]),
                "lon": float(row["longitude"]),
            }
    return None


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_oni_data() -> pd.DataFrame:
    response = requests.get(ONI_SOURCE_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    dfs = pd.read_html(io.StringIO(response.text))
    if not dfs:
        raise ValueError("ไม่พบตารางข้อมูล ONI จากแหล่งข้อมูล NOAA")

    df_oni_raw = None
    for table in dfs:
        columns = [str(c).strip() for c in table.columns]
        if "Year" in columns or (not table.empty and str(table.iloc[0, 0]).strip() == "Year"):
            df_oni_raw = table.copy()
            break

    if df_oni_raw is None:
        raise ValueError("ไม่พบตาราง ONI ที่มีคอลัมน์ Year")

    if str(df_oni_raw.iloc[0, 0]).strip() == "Year":
        df_oni_raw.columns = df_oni_raw.iloc[0]
        df_oni_raw = df_oni_raw[1:]

    df_oni_raw = df_oni_raw[df_oni_raw["Year"] != "Year"].copy()

    season_to_month = {
        "DJF": 1,
        "JFM": 2,
        "FMA": 3,
        "MAM": 4,
        "AMJ": 5,
        "MJJ": 6,
        "JJA": 7,
        "JAS": 8,
        "ASO": 9,
        "SON": 10,
        "OND": 11,
        "NDJ": 12,
    }

    df_oni = df_oni_raw.melt(id_vars=["Year"], var_name="Season", value_name="ONI_Index")
    df_oni["Month"] = df_oni["Season"].map(season_to_month)
    df_oni = df_oni.dropna(subset=["Month", "ONI_Index"])
    df_oni = df_oni[df_oni["ONI_Index"] != ""]
    df_oni["ONI_Index"] = pd.to_numeric(df_oni["ONI_Index"], errors="coerce")
    df_oni["year_month"] = df_oni.apply(lambda row: f"{int(row['Year'])}-{int(row['Month']):02d}", axis=1)

    result = df_oni[["year_month", "ONI_Index"]].sort_values("year_month").reset_index(drop=True)
    if result.empty:
        raise ValueError("ตาราง ONI ว่างหลังจากทำความสะอาดข้อมูล")
    return result


def _fetch_with_retry(station_id: str, start_date, query_end_date, logs: list, label: str):
    """Returns (dataframe or None, reason). Network errors are retried; an empty answer is final."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_daily_dataframe(station_id, start_date, query_end_date), "success"
        except EmptyStationData:
            logs.append(f"{label} {station_id}: empty")
            return None, "empty"
        except Exception as e:
            logs.append(f"{label} {station_id} attempt {attempt}: {type(e).__name__}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return None, "exception"


def fetch_station_with_retry(wmo_id: str, info: Dict[str, Any], start_date, query_end_date) -> Dict[str, Any]:
    logs: list = []
    station_df, station_reason = _fetch_with_retry(wmo_id, start_date, query_end_date, logs, "WMO")
    source = {"id": wmo_id, "name": info["name"], "lat": info["lat"], "lon": info["lon"]}
    station_source = "wmo"

    if station_df is None:
        try:
            fallback = find_fallback_station(wmo_id, info["lat"], info["lon"])
        except Exception as e:
            fallback = None
            logs.append(f"Fallback lookup failed: {type(e).__name__}: {e}")
        if fallback is None:
            logs.append("Fallback lookup: no other station within 50 km")
        else:
            station_df, station_reason = _fetch_with_retry(fallback["id"], start_date, query_end_date, logs, "Fallback")
            if station_df is not None:
                source, station_source = fallback, "fallback"

    if station_df is not None:
        station_df = station_df.copy()
        station_df["wmo_id"] = wmo_id
        station_df["source_id"] = source["id"]
        station_df["source"] = station_source

        if station_source == "wmo":
            thai_source = "สถานีหลัก"
            thai_detail = "ดึงข้อมูลจากสถานีหลักสำเร็จ"
        else:
            thai_source = "สถานีใกล้เคียง (Fallback)"
            thai_detail = f"สถานีหลักไม่มีข้อมูล จึงใช้ข้อมูลจาก {source['name']} ({source['id']}) แทน"

        return {
            "ok": True,
            "weather_df": station_df,
            "source_id": source["id"],
            "fetched_station": {
                "ชื่อสถานี": info["name"],
                "ที่อยู่": info["address"],
                "ภูมิภาค": info["region"],
                # The map shows where the data really comes from.
                "ละติจูด": source["lat"],
                "ลองจิจูด": source["lon"],
                "รหัสสถานี (WMO)": wmo_id if station_source == "wmo" else f"{wmo_id} → {source['id']}",
                "แหล่งข้อมูล": thai_source,
                "source": station_source,
            },
            "station_result": {
                "รหัสสถานี": wmo_id,
                "ชื่อสถานี": info["name"],
                "สถานะ": "สำเร็จ",
                "แหล่งที่มา": thai_source,
                "รายละเอียดเพิ่มเติม": thai_detail,
            },
        }

    detail = "; ".join(logs[-3:]) if logs else "no detail"
    if "timeout" in detail.lower():
        final_reason, thai_detail = "timeout", "เชื่อมต่อเกินเวลาที่กำหนด (Timeout)"
    elif station_reason == "empty":
        final_reason, thai_detail = "empty", "ช่วงเวลาดังกล่าวไม่มีข้อมูลในฐานข้อมูล"
    else:
        final_reason, thai_detail = "exception", "เกิดความผิดพลาดในการเชื่อมต่อ API"

    return {
        "ok": False,
        "failed_station": {"wmo_id": wmo_id, "name": info["name"], "reason": final_reason, "detail": detail},
        "station_result": {
            "รหัสสถานี": wmo_id,
            "ชื่อสถานี": info["name"],
            "สถานะ": "ล้มเหลว",
            "แหล่งที่มา": "-",
            "รายละเอียดเพิ่มเติม": thai_detail,
        },
    }


def fetch_stations_parallel(wmo_stations, start_date, query_end_date, progress_bar, status_text):
    all_weather_data = []
    fetched_stations = []
    failed_stations = []
    station_results = []
    results = []

    station_items = list(wmo_stations.items())
    total_stations = len(station_items)
    max_workers = min(MAX_PARALLEL_WORKERS, total_stations) if total_stations > 0 else 1

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_station = {
            executor.submit(fetch_station_with_retry, wmo_id, info, start_date, query_end_date): (wmo_id, info)
            for wmo_id, info in station_items
        }

        completed = 0
        for future in as_completed(future_to_station):
            wmo_id, info = future_to_station[future]
            completed += 1
            status_text.text(f"กำลังดำเนินการสืบค้นข้อมูลสถานี: {info['name']} ({completed}/{total_stations})")

            try:
                result = future.result()
            except Exception as e:
                result = {
                    "ok": False,
                    "failed_station": {
                        "wmo_id": wmo_id,
                        "name": info["name"],
                        "reason": "exception",
                        "detail": f"Unhandled error: {type(e).__name__}: {str(e)}",
                    },
                    "station_result": {
                        "รหัสสถานี": wmo_id,
                        "ชื่อสถานี": info["name"],
                        "สถานะ": "ล้มเหลว",
                        "แหล่งที่มา": "-",
                        "รายละเอียดเพิ่มเติม": "เกิดความผิดพลาดในการเชื่อมต่อ API",
                    },
                }

            results.append(result)
            progress_bar.progress(completed / total_stations)

    # Primary stations first, so a station's own data wins over another station's fallback copy of it.
    results.sort(key=lambda r: not (r["ok"] and r["fetched_station"]["source"] == "wmo"))
    used_sources = set()
    for result in results:
        if result["ok"] and result["source_id"] in used_sources:
            result = {
                "ok": False,
                "failed_station": {
                    "wmo_id": result["station_result"]["รหัสสถานี"],
                    "name": result["station_result"]["ชื่อสถานี"],
                    "reason": "duplicate",
                    "detail": f"fallback {result['source_id']} already counted",
                },
                "station_result": {
                    **result["station_result"],
                    "สถานะ": "ไม่นับซ้ำ",
                    "รายละเอียดเพิ่มเติม": f"สถานีสำรอง {result['source_id']} ถูกนับแล้ว จึงไม่นำมาเฉลี่ยซ้ำ",
                },
            }
        if result["ok"]:
            used_sources.add(result["source_id"])
            all_weather_data.append(result["weather_df"])
            fetched_stations.append(result["fetched_station"])
        else:
            failed_stations.append(result["failed_station"])
        station_results.append(result["station_result"])

    return all_weather_data, fetched_stations, failed_stations, station_results
