import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_processing import build_monthly_weather_dataframe, interpolate_short_gaps  # noqa: E402


def test_short_interior_gap_is_filled_long_one_and_edges_are_not():
    s = pd.Series([np.nan, 1.0, np.nan, 3.0] + [np.nan] * 15 + [5.0, np.nan])
    out = interpolate_short_gaps(s, max_gap=14)
    assert out[2] == 2.0                      # 1-day gap filled linearly
    assert out[4:19].isna().all()             # 15-day gap left empty, not partly filled
    assert np.isnan(out[0]) and np.isnan(out[20])  # edges untouched


def _station(wmo_id, prcp):
    idx = pd.date_range("2025-01-01", periods=len(prcp), freq="D", name="time")
    return pd.DataFrame({"temp": 27.0, "prcp": prcp, "wmo_id": wmo_id}, index=idx)


def test_missing_rain_is_not_counted_as_zero():
    # Station B has no rain reading on day 1; the mean must use station A only.
    monthly = build_monthly_weather_dataframe([_station("A", [10.0, 0.0]), _station("B", [np.nan, 0.0])])
    assert monthly.loc[0, "prcp_sum"] == 10.0
    assert monthly.loc[0, "rainy_days"] == 1


def test_month_without_any_rain_data_stays_empty():
    monthly = build_monthly_weather_dataframe([_station("A", [np.nan, np.nan])])
    assert monthly["prcp_sum"].isna().all()
