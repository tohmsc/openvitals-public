# Device adapter — bring your own wearable's data

The dashboard doesn't care where data comes from. WHOOP 4.0 owners get it filled
automatically by the pipeline (`collector.py`). **Everyone else** (Oura, Garmin, Apple
Watch, Fitbit, Polar, an older WHOOP export, a spreadsheet you kept by hand) fills these
CSV templates and runs one command:

```bash
cp -r templates my_data          # my_data/ is yours — *.csv outside templates/ is gitignored
# ...replace the example rows with your export (or have your agent do it)...
.venv/bin/python import_data.py --dir my_data --dry-run   # validate
.venv/bin/python import_data.py --dir my_data             # writes whoop.db
.venv/bin/python dashboard.py                             # http://localhost:8787
```

The example rows in these files are **fictional**. Re-running the import is safe (upserts).

## Columns

Only the first column of each file is required. **Leave anything you don't have blank** —
the dashboard just hides that metric.

### `daily.csv` — one row per day
| column | meaning | unit |
|---|---|---|
| `date` | **required** — the day the score belongs to (wake-up day) | `YYYY-MM-DD` |
| `recovery_pct` | readiness / recovery / body battery score | 0–100 |
| `hrv_ms` | overnight HRV, **RMSSD** | ms |
| `resting_hr` | resting heart rate | bpm |
| `spo2_pct` | overnight blood oxygen | % |
| `skin_temp_c` | **absolute** skin temp (not a ± deviation) | °C |
| `resp_rate` | overnight respiratory rate | breaths/min |
| `sleep_start`, `sleep_end` | main sleep window | ISO date-time |
| `light_min`, `deep_min`, `rem_min`, `awake_min` | sleep stage totals | minutes |
| `sleep_perf_pct` | sleep score / performance | 0–100 |
| `disturbances` | wake-ups during sleep | count |
| `strain` | day strain / load (WHOOP scale 0–21; rescale others) | 0–21 |
| `avg_hr`, `max_hr` | day heart rate | bpm |
| `kilojoules` | energy burned (kcal × 4.184) | kJ |

### `workouts.csv` — one row per activity
`start` (**required**, ISO date-time), `end`, `sport` (free text: running, cycling…),
`strain` (0–21), `avg_hr`, `max_hr`, `kilojoules`, `distance_m`.

### `heart_rate.csv` — optional HR samples
`timestamp` (**required**, ISO date-time; `Z`/offsets are converted to UTC), `hr` (bpm).
Shows on the live heart-rate chart.

## Mapping common exports (hints for your agent)
| Device | Where to get it | Notes |
|---|---|---|
| Oura | Oura web → Export data (CSV) or API v2 `daily_readiness`, `sleep` | readiness → `recovery_pct`; `average_hrv` is RMSSD; temp is a deviation, so leave `skin_temp_c` blank unless you have absolute |
| Garmin | Garmin Connect → Export / GDPR data download | Body Battery high or HRV-status → `recovery_pct`; Training Load ÷ ~10 ≈ `strain` |
| Apple Health | Health app → Export All Health Data (`export.xml`) | aggregate `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` per night (SDNN, close enough), `RestingHeartRate`, `OxygenSaturation`, sleep stages from `SleepAnalysis` |
| Fitbit | Google Takeout → Fitbit | Daily Readiness → `recovery_pct`; HRV `rmssd`; sleep stages in minutes |
| WHOOP (any) | app → Export, or the official API | columns map 1:1 |

## Prompt to hand your agent
> Read `whoop-tools/templates/README.md`. Convert my export at `<path>` into
> `my_data/daily.csv`, `my_data/workouts.csv` (and `heart_rate.csv` if available) using
> exactly those headers and units, leaving unknown cells blank. Run
> `import_data.py --dir my_data --dry-run`, fix any skipped rows, then import for real and
> start the dashboard.

Never commit your filled-in CSVs or `whoop.db` — they're your health data.
