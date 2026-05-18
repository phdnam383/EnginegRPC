import pandas as pd
import numpy as np
from pathlib import Path
from typing import List, Dict, Any, Optional

# ── Constants ──────────────────────────────────────────────────────────────────
DATA_PATH         = Path("data/ims_alerts.csv")
TIME_GAP_SECONDS  = 10    # Maximum gap (seconds) between consecutive alarms in a storm
STORM_THRESHOLD   = 15    # Minimum alarm count to qualify as a storm

# Epoch anchor – version-safe reference point
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _to_unix_seconds(series: pd.Series) -> pd.Series:
    """
    Convert a datetime Series to integer Unix seconds.

    Robust against pandas version differences:
      • pandas < 2.0  stores datetime64[ns]  → .astype(int64) gives nanoseconds
      • pandas ≥ 2.0  stores datetime64[us]  → .astype(int64) gives microseconds

    Using (ts - epoch).dt.total_seconds() avoids the ambiguity entirely and
    always returns seconds as float64; we then cast to int64.
    """
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    return (parsed - _EPOCH).dt.total_seconds().astype("Int64")   # nullable int64; NaT → <NA>


# ── Core functions ─────────────────────────────────────────────────────────────
def load_data(path: Path = DATA_PATH) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df["created_at"] = _to_unix_seconds(df["created_at"])
    df["closed_at"]  = _to_unix_seconds(df["closed_at"])   # NaT rows become <NA>
    return df


def detect_alarm_storms(
    df: pd.DataFrame,
    time_gap: int = TIME_GAP_SECONDS,
    threshold: int = STORM_THRESHOLD,
) -> List[List[Dict[str, Any]]]:
    df_sorted = df.sort_values("created_at").reset_index(drop=True)
    records   = df_sorted.to_dict("records")

    storms: List[List[Dict[str, Any]]] = []
    current_storm: List[Dict[str, Any]] = [records[0]]

    for i in range(1, len(records)):
        gap = records[i]["created_at"] - records[i - 1]["created_at"]
        if gap <= time_gap:
            current_storm.append(records[i])
        else:
            if len(current_storm) >= threshold:
                storms.append(current_storm)
            current_storm = [records[i]]

    # last storm
    if len(current_storm) >= threshold:
        storms.append(current_storm)

    return storms


def storm_summary(storms: List[List[Dict[str, Any]]]) -> pd.DataFrame:
    rows = []
    for idx, storm in enumerate(storms):
        times = [r["created_at"] for r in storm]
        rows.append({
            "storm_id"   : idx,
            "n_alarms"   : len(storm),
            "start_unix" : min(times),
            "end_unix"   : max(times),
            "duration_s" : max(times) - min(times),
            "start_dt"   : pd.Timestamp(min(times), unit="s", tz="UTC"),
            "end_dt"     : pd.Timestamp(max(times), unit="s", tz="UTC"),
        })
    return pd.DataFrame(rows)


# ── Diagnostics ────────────────────────────────────────────────────────────────
def _diagnose_timestamps(df: pd.DataFrame) -> None:
    """Print a quick sanity-check so timestamp bugs surface immediately."""
    sample = df["created_at"].dropna().iloc[0]
    dt = pd.Timestamp(int(sample), unit="s", tz="UTC")
    diffs = df["created_at"].dropna().sort_values().diff().dropna()
    print(f"  First alarm Unix ts : {int(sample):,}  →  {dt}")
    print(f"  Inter-alarm gap stats (seconds):")
    print(f"    min={diffs.min():.0f}  median={diffs.median():.1f}"
          f"  mean={diffs.mean():.1f}  max={diffs.max():.0f}")


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("ALARM STORM DETECTION")
    print(f"  TIME_GAP_SECONDS = {TIME_GAP_SECONDS}s")
    print(f"  STORM_THRESHOLD  = {STORM_THRESHOLD} alarms")
    print(f"  pandas version   = {pd.__version__}")
    print("=" * 60)

    df = load_data()
    print(f"\nLoaded {len(df):,} alarms.")

    print("\n── Timestamp sanity check ──────────────────────────────────")
    _diagnose_timestamps(df)

    print("\n── Storm detection ─────────────────────────────────────────")
    storms  = detect_alarm_storms(df)
    summary = storm_summary(storms)

    print(f"Detected {len(storms):,} valid alarm storms (≥ {STORM_THRESHOLD} alarms).")

    print("\nStorm size distribution:")
    print(summary["n_alarms"].describe().to_string())

    total_in_storms = summary["n_alarms"].sum()
    print(f"\nTotal alarms inside storms  : {total_in_storms:,}")
    print(f"Percentage in storms        : {total_in_storms / len(df) * 100:.1f}%")

    print("\n── Top 5 storms (most alarms) ──────────────────────────────")
    cols = ["storm_id", "n_alarms", "duration_s", "start_dt", "end_dt"]
    print(summary.nlargest(5, "n_alarms")[cols].to_string(index=False))

    print("\n── Storm size buckets ──────────────────────────────────────")
    buckets = [
        ("mega  (≥ 1 000)", summary["n_alarms"] >= 1000),
        ("large (100–999)", (summary["n_alarms"] >= 100) & (summary["n_alarms"] < 1000)),
        ("small ( 15–99)", summary["n_alarms"] < 100),
    ]
    for label, mask in buckets:
        print(f"  {label}: {mask.sum():>4} storms")