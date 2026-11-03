"""Synthetic monthly batches of machine telemetry, with controllable drift.

Each row is one machine-day: daily sensor aggregates for a pump, compressor or motor, plus the
label ``failure_within_7d``. A batch is one month of data for a fleet.

Why synthetic: the platform's job is to *notice and handle change*, so the data has to change in
known ways. Each month can apply a drift scenario:

Scenarios combine with ``+`` (``"sensor_drift+fleet_mix"``), because real changes persist.

=================  ===========================================================================
``none``           the reference behaviour
``sensor_drift``   the vibration sensors on pumps are recalibrated badly: +40% offset (covariate
                   drift; the physics hasn't changed, the measurements have)
``fleet_mix``      the fleet shifts towards compressors (population drift)
``new_failure``    a new failure mode appears: seal leaks, visible in pressure and current rather
                   than vibration (concept drift; the old model can't see it)
=================  ===========================================================================

Everything is driven by a seed, so every batch is reproducible.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

EQUIPMENT_TYPES = ("pump", "compressor", "motor")
SCENARIOS = ("none", "sensor_drift", "fleet_mix", "new_failure")  # combine with "+": "sensor_drift+fleet_mix"
FEATURES_NUMERIC = (
    "age_days",
    "days_since_service",
    "load_pct",
    "ambient_temp",
    "vibration_mean",
    "vibration_std",
    "vibration_trend",
    "bearing_temp_rise",
    "current_mean",
    "pressure_mean",
)
FEATURES_CATEGORICAL = ("equipment_type",)
LABEL = "failure_within_7d"
KEYS = ("unit_id", "event_date")

# Healthy daily levels per equipment type: vibration (mm/s), bearing temp rise (°C), current (A), pressure (bar).
BASE = {
    "pump": (2.2, 18.0, 42.0, 6.5),
    "compressor": (3.0, 24.0, 95.0, 8.0),
    "motor": (1.8, 15.0, 30.0, 0.0),
}


@dataclass(frozen=True)
class BatchSpec:
    month: str  # "YYYY-MM"
    scenario: str = "none"
    units: int = 240
    seed: int = 7

    def __post_init__(self) -> None:
        unknown = [p for p in self.scenario.split("+") if p not in SCENARIOS]
        if unknown:
            raise ValueError(f"unknown scenario {'+'.join(unknown)}; combine {SCENARIOS} with '+'")

    @property
    def effects(self) -> set[str]:
        return set(self.scenario.split("+")) - {"none"}


def _month_days(month: str) -> list[date]:
    start = date.fromisoformat(f"{month}-01")
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return [start + timedelta(days=i) for i in range((nxt - start).days)]


def generate_batch(spec: BatchSpec) -> pd.DataFrame:
    """One month of machine-days. Degradation is latent; sensors show it with noise and confounders."""
    # crc32, not hash(): Python randomizes string hashes per process, which would break reproducibility.
    rng = np.random.default_rng(zlib.crc32(f"{spec.seed}|{spec.month}|{spec.scenario}".encode()))
    days = _month_days(spec.month)
    fx = spec.effects
    type_p = [0.25, 0.55, 0.20] if "fleet_mix" in fx else [0.45, 0.25, 0.30]
    types = rng.choice(EQUIPMENT_TYPES, size=spec.units, p=type_p)
    unit_ids = [f"{t[:1].upper()}-{spec.seed % 100:02d}{i:04d}" for i, t in enumerate(types)]
    rows = []
    for uid, etype in zip(unit_ids, types, strict=True):
        vib0, temp0, cur0, pres0 = BASE[etype]
        tol = rng.normal(1.0, 0.06, size=4)  # manufacturing tolerance
        age0 = int(rng.integers(60, 3000))
        service0 = int(rng.integers(0, 75))
        # ~18% of units are in a degradation cycle that ends in failure at some day (possibly after the month).
        fails_on = int(rng.integers(5, len(days) + 10)) if rng.random() < 0.18 else None
        # Seal leaks affect machines with seals (pumps, compressors), not motors.
        seal_mode = "new_failure" in fx and etype != "motor" and fails_on is not None and rng.random() < 0.6
        # Confounder: preventive maintenance catches ~35% of degrading units 1-5 days before they would
        # fail. Those days *look* like a developing failure but are labelled healthy, as in real fleets.
        repaired_on = fails_on - int(rng.integers(1, 6)) if fails_on is not None and rng.random() < 0.35 else None
        for d_idx, day in enumerate(days):
            to_failure = None if fails_on is None else fails_on - d_idx
            if to_failure is not None and to_failure < 0:
                continue  # unit is down after failing
            repaired = repaired_on is not None and d_idx >= repaired_on
            deg = 0.0 if to_failure is None or repaired else float(np.exp(-to_failure / 4.5))
            if repaired:
                to_failure = None  # the repair cancelled the failure
            load = float(np.clip(rng.normal(70, 15), 15, 105))
            ambient = float(rng.normal(22, 4))  # climate-controlled plant: no seasonal swing
            transient = rng.random() < 0.08  # process upsets: noisy but harmless
            if seal_mode:
                vib = vib0 * tol[0] * (1 + 0.08 * deg) + (1.5 if transient else 0)
                pres = pres0 * tol[3] * (1 - 0.6 * deg)
                cur = cur0 * tol[2] * (1 + 0.55 * deg) * (load / 70) ** 0.5
                temp = temp0 * tol[1] * (1 + 0.10 * deg)
            else:
                vib = vib0 * tol[0] * (1 + 0.6 * deg) + (1.5 if transient else 0)
                pres = pres0 * tol[3] * (1 - 0.05 * deg)
                cur = cur0 * tol[2] * (1 + 0.06 * deg) * (load / 70) ** 0.5
                temp = temp0 * tol[1] * (1 + 0.25 * deg)
            if "sensor_drift" in fx and etype == "pump":
                vib *= 1.4
            noise = rng.normal(1.0, 0.12, size=4)
            row = {
                "unit_id": uid,
                "event_date": day,
                "equipment_type": etype,
                "age_days": age0 + d_idx,
                "days_since_service": service0 + d_idx,
                "load_pct": round(load, 1),
                "ambient_temp": round(ambient, 1),
                "vibration_mean": round(vib * noise[0], 3),
                "vibration_std": round(abs(vib * (0.08 + (0.02 if seal_mode else 0.15) * deg) * rng.normal(1, 0.2)), 3),
                "vibration_trend": round(float(rng.normal(0.3 * deg if not seal_mode else 0.03 * deg, 0.15)), 3),
                "bearing_temp_rise": round(temp * noise[1], 2),
                "current_mean": round(cur * noise[2], 2),
                "pressure_mean": round(max(pres * noise[3], 0.0), 3),
                LABEL: int(to_failure is not None and to_failure <= 7),
            }
            rows.append(row)
    df = pd.DataFrame(rows)
    # Real telemetry has gaps: ~1% of sensor readings missing.
    for col in ("vibration_std", "bearing_temp_rise", "current_mean"):
        df.loc[rng.random(len(df)) < 0.01, col] = np.nan
    df.loc[df["equipment_type"] == "motor", "pressure_mean"] = np.nan  # motors have no pressure sensor
    df["event_date"] = pd.to_datetime(df["event_date"])
    return df.sort_values(["event_date", "unit_id"]).reset_index(drop=True)


def default_timeline() -> list[BatchSpec]:
    """The demo year: six stable months, then changes that *persist* once they start, as in a real plant:
    sensor drift from July, a fleet shift from September, a new failure mode from October."""
    sd, fm, nf = "sensor_drift", "sensor_drift+fleet_mix", "sensor_drift+fleet_mix+new_failure"
    plan = ["none"] * 6 + [sd, sd, fm, nf, nf, nf]
    return [BatchSpec(month=f"2026-{m:02d}", scenario=s) for m, s in zip(range(1, 13), plan, strict=True)]
