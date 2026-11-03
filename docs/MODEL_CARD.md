# Model card: machine failure within 7 days

## Purpose

Rank equipment by its risk of failing in the next 7 days, so maintenance crews can inspect the riskiest
machines first. The model raises an alert when the calibrated probability is above a threshold chosen
for **≥ 50% precision**, meaning at least one in two alerts should be a real developing failure.

**Not for:** safety-critical shutdown decisions, or equipment types it was not trained on (unknown types
are rejected by the data contract and by the serving schema). It does not replace inspection; it decides
where to look first.

## Data

Synthetic telemetry from [generator.py](../backend/app/ml/generator.py). Each row is one **machine-day**.
- **Fleet:** 240 machines (pumps 45%, compressors 25%, motors 30% in the base mix), about 7,000 rows a
  month, and a failure-within-7-days rate of about 3%.
- **Degradation:** about 18% of machines go through a degradation cycle that ends in failure. The
  signals rise exponentially as failure approaches. Most failure modes show in vibration and bearing
  temperature; seal leaks (pumps and compressors) show in pressure and motor current.
- **Realism:** manufacturing tolerance per machine, 12% sensor noise, 8% harmless transient spikes, about
  1% missing readings, and motors with no pressure sensor. Preventive maintenance catches about 35% of
  degrading machines 1–5 days early. Those days *look* like a developing failure but are labelled
  healthy, as in real fleets.
- **Label:** `failure_within_7d`, which is 1 if the machine fails within the next 7 days.

| Feature | Meaning |
|---|---|
| `equipment_type` | pump, compressor, motor (one-hot) |
| `age_days`, `days_since_service` | machine age, time since the last service |
| `load_pct`, `ambient_temp` | operating context |
| `vibration_mean`, `vibration_std`, `vibration_trend` | daily vibration aggregates and the 7-day slope |
| `bearing_temp_rise` | bearing temperature above ambient |
| `current_mean` | motor current |
| `pressure_mean` | discharge pressure (missing on motors) |

## Training and evaluation

- XGBoost (300 trees, depth 4) inside a scikit-learn pipeline, with isotonic calibration (3-fold).
- **Time-based splits only.** The model trains on four consecutive months and is evaluated on the
  following month. Rows of the same machine are strongly correlated, so a random split would leak and
  inflate every metric.
- **Metrics:**
  - PR-AUC (average precision), the primary metric
  - ROC-AUC
  - Brier score
  - expected calibration error
  - precision, recall and alert volume at the threshold
  - recall and precision per equipment type
  - single-row p95 latency

### Results (first model, trained Feb–May, evaluated on June)

| PR-AUC | Recall at threshold | Precision at threshold | ECE |
|---|---|---|---|
| 0.80 | 84% | 53% | < 0.01 |

The 7-day horizon sets a ceiling. Early in a degradation cycle the signals are still within normal
variation, and preventive repairs make some degrading days healthy by label. A PR-AUC of about 0.8 is
therefore close to what this data allows.

### Under drift (the replayed year; [lifecycle report](../eval/reports/lifecycle.md))

| Change | Effect on the model trained before it | After the platform's retrain |
|---|---|---|
| Pump vibration sensors read +40% | PR-AUC 0.80 → 0.72; **precision 53% → 26%** (false alarms on healthy pumps) | 0.77 (v3, then v4) |
| Fleet shifts toward compressors | slices shift; handled by the next window | — |
| New failure mode: seal leaks | **PR-AUC → 0.39**, recall 34% | 0.69 by December (v6) |

## Limits and risks

- **Labels arrive after 7 days.** Performance monitoring always lags the data by at least a week. Drift
  monitoring is immediate but cannot see concept drift.
- **A new failure mode is invisible until it has happened often enough to learn.** In the replay, the
  first retrain after seal leaks appeared was rightly blocked (PR-AUC below the floor and weak recall on
  one fleet). It took a second month of data to produce a model worth deploying.
- **One threshold for all equipment types.** Precision differs by fleet, and a per-type threshold would
  serve crews better.
- **Synthetic data.** The degradation physics is simplified. Real deployments need a backtest on the
  fleet's own history before any of these numbers mean anything.
