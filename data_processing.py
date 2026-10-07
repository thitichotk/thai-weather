from __future__ import annotations

import io
from typing import List

import numpy as np
import pandas as pd

INTERPOLATE_LIMIT_DAYS = 14


def to_excel(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    return output.getvalue()


def classify_oni(oni_value):
    if pd.isna(oni_value):
        return "ไม่สามารถจัดประเภทได้"

    if oni_value >= 2.0:
        return "เอลนีโญรุนแรงมาก (Very Strong/Super El Niño)"
    if oni_value >= 1.5:
        return "เอลนีโญรุนแรง (Strong)"
    if oni_value >= 1.0:
        return "เอลนีโญปานกลาง (Moderate)"
    if oni_value >= 0.5:
        return "เอลนีโญอ่อน (Weak)"
    if oni_value <= -2.0:
        return "ลานีญารุนแรงมาก (Very Strong)"
    if oni_value <= -1.5:
        return "ลานีญารุนแรง (Strong)"
    if oni_value <= -1.0:
        return "ลานีญาปานกลาง (Moderate)"
    if oni_value <= -0.5:
        return "ลานีญาอ่อน (Weak)"
    return "สภาวะเป็นกลาง (Neutral)"


def interpolate_short_gaps(col: pd.Series, max_gap: int = INTERPOLATE_LIMIT_DAYS) -> pd.Series:
    """Linear fill for interior gaps of at most `max_gap` days; longer gaps and the edges stay empty."""
    filled = col.interpolate(method="linear", limit_area="inside")
    gap_len = col.isna().groupby(col.notna().cumsum()).transform("sum")
    return filled.where(col.notna() | (gap_len <= max_gap))


def build_monthly_weather_dataframe(all_weather_data: List[pd.DataFrame]) -> pd.DataFrame:
    if not all_weather_data:
        raise ValueError("ไม่มีข้อมูลสถานีที่สามารถนำมารวมได้")

    df_raw = pd.concat(all_weather_data, ignore_index=False)
    df_raw = df_raw.drop(columns=["snow", "wpgt", "tsun"], errors="ignore")
    df_raw = df_raw.reset_index().rename(columns={"time": "date"})

    if "date" not in df_raw.columns:
        raise ValueError("ข้อมูลดิบไม่มีคอลัมน์วันที่ (date/time)")

    df_raw["date"] = pd.to_datetime(df_raw["date"], errors="coerce")
    df_raw = df_raw.dropna(subset=["date"])
    if df_raw.empty:
        raise ValueError("ไม่พบข้อมูลวันที่ที่ถูกต้องหลังจากทำความสะอาด")

    # Missing rain stays missing: counting it as 0 mm would pull the averages down.
    df_raw["prcp"] = pd.to_numeric(df_raw.get("prcp"), errors="coerce")

    cols_to_interp = ["temp", "tmin", "tmax", "rhum", "pres", "wspd"]
    for col in cols_to_interp:
        if col not in df_raw.columns:
            df_raw[col] = np.nan
        df_raw[col] = pd.to_numeric(df_raw[col], errors="coerce")

    # Interpolate short gaps per station in chronological order. Sorting first
    # guarantees linear interpolation follows the time axis, and transform keeps
    # the result aligned to the original rows without index gymnastics.
    df_raw = df_raw.sort_values(["wmo_id", "date"])
    df_raw[cols_to_interp] = df_raw.groupby("wmo_id")[cols_to_interp].transform(interpolate_short_gaps)

    df_daily = df_raw.groupby("date", as_index=False).agg(
        {
            "temp": "mean",
            "tmin": "mean",
            "tmax": "mean",
            "rhum": "mean",
            "prcp": "mean",
            "wspd": "mean",
            "pres": "mean",
        }
    )

    df_daily["year_month"] = df_daily["date"].dt.to_period("M")
    df_monthly = df_daily.groupby("year_month", as_index=False).agg(
        temp_mean=("temp", "mean"),
        tmax_max=("tmax", "max"),
        tmin_min=("tmin", "min"),
        rhum_mean=("rhum", "mean"),
        wspd_mean=("wspd", "mean"),
        pres_mean=("pres", "mean"),
        prcp_sum=("prcp", lambda x: x.sum(min_count=1)),
        rainy_days=("prcp", lambda x: (x > 0.5).sum() if x.notna().any() else np.nan),
    )

    # Fill only the leading/trailing edges of the series; interior gaps and
    # fully-empty indicators stay NaN so nothing is guessed in the middle.
    df_monthly = df_monthly.bfill(limit_area="outside").ffill(limit_area="outside")
    df_monthly["year_month"] = df_monthly["year_month"].astype(str)
    return df_monthly


def merge_oni_labels(df_monthly: pd.DataFrame, df_oni: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(df_monthly, df_oni, on="year_month", how="left")
    if "ONI_Index" in merged.columns:
        merged["ONI_Label"] = merged["ONI_Index"].apply(classify_oni)
    return merged
