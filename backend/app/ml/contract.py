"""The data contract every batch must satisfy before it can be used for training or scoring.

Bad data is the most common cause of a bad model in production, and it fails silently: a unit
change or a broken sensor feed still produces numbers. The contract turns those into a blocked
pipeline run with a readable report.

Hard failures (block the batch): missing columns, wrong categories, physically impossible values,
duplicate keys, too many missing values, an implausible label rate, too few rows.
Warnings (recorded, do not block): a column's median moving far from the contract's expected range.
Distribution drift is measured separately, by ``drift.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from app.ml.generator import EQUIPMENT_TYPES, KEYS, LABEL

# column -> (min, max): physically possible range. Values outside are sensor or pipeline errors.
RANGES: dict[str, tuple[float, float]] = {
    "age_days": (0, 20000),
    "days_since_service": (0, 2000),
    "load_pct": (0, 120),
    "ambient_temp": (-40, 60),
    "vibration_mean": (0, 50),
    "vibration_std": (0, 30),
    "vibration_trend": (-5, 5),
    "bearing_temp_rise": (-10, 120),
    "current_mean": (0, 500),
    "pressure_mean": (0, 30),
}
# Typical medians: a median outside this band is worth a look (warning, not a failure).
TYPICAL_MEDIAN: dict[str, tuple[float, float]] = {
    "vibration_mean": (1.0, 5.0),
    "bearing_temp_rise": (10.0, 35.0),
    "current_mean": (20.0, 110.0),
}
MAX_NULL_RATE = 0.05
LABEL_RATE = (0.005, 0.25)
MIN_ROWS = 500
REQUIRED = (*KEYS, "equipment_type", *RANGES, LABEL)


@dataclass
class ContractReport:
    passed: bool
    rows: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate(df: pd.DataFrame, *, labelled: bool = True) -> ContractReport:
    errors: list[str] = []
    warnings: list[str] = []
    required = REQUIRED if labelled else tuple(c for c in REQUIRED if c != LABEL)
    missing = [c for c in required if c not in df.columns]
    if missing:
        return ContractReport(False, len(df), [f"missing columns: {', '.join(missing)}"])

    if len(df) < MIN_ROWS:
        errors.append(f"only {len(df)} rows (minimum {MIN_ROWS})")
    dupes = int(df.duplicated(subset=list(KEYS)).sum())
    if dupes:
        errors.append(f"{dupes} duplicate (unit_id, event_date) rows")
    unknown = sorted(set(df["equipment_type"].dropna()) - set(EQUIPMENT_TYPES))
    if unknown:
        errors.append(f"unknown equipment types: {', '.join(map(str, unknown))}")

    null_rates: dict[str, float] = {}
    for col, (lo, hi) in RANGES.items():
        values = pd.to_numeric(df[col], errors="coerce")
        applicable = values[df["equipment_type"] != "motor"] if col == "pressure_mean" else values
        rate = float(applicable.isna().mean()) if len(applicable) else 0.0
        null_rates[col] = round(rate, 4)
        if rate > MAX_NULL_RATE:
            errors.append(f"{col}: {rate:.1%} missing (maximum {MAX_NULL_RATE:.0%})")
        out = int(((values < lo) | (values > hi)).sum())
        if out:
            errors.append(f"{col}: {out} values outside the physical range [{lo}, {hi}]")
        if col in TYPICAL_MEDIAN and values.notna().any():
            med = float(values.median())
            t_lo, t_hi = TYPICAL_MEDIAN[col]
            if not t_lo <= med <= t_hi:
                warnings.append(f"{col}: median {med:.2f} is outside the typical range [{t_lo}, {t_hi}]")

    stats: dict[str, Any] = {
        "null_rates": null_rates,
        "equipment_mix": df["equipment_type"].value_counts(normalize=True).round(3).to_dict(),
    }
    if labelled:
        rate = float(df[LABEL].mean())
        stats["label_rate"] = round(rate, 4)
        if set(pd.unique(df[LABEL])) - {0, 1}:
            errors.append(f"{LABEL} must be 0 or 1")
        if not LABEL_RATE[0] <= rate <= LABEL_RATE[1]:
            errors.append(f"label rate {rate:.2%} outside the plausible range {LABEL_RATE[0]:.1%}–{LABEL_RATE[1]:.0%}")
    return ContractReport(not errors, len(df), errors, warnings, stats)
