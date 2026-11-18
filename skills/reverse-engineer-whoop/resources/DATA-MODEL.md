# Data model (`whoop.db`, SQLite)

One file, gitignored. Two families of tables: what the strap gives you (`ble_*`, decoded
`realtime_hr`, `device_*`, `strap_samples`), and what the WHOOP API returns (`api_*`).

| Table | Filled by | What it holds | Freshness |
| --- | --- | --- | --- |
| `ble_hr` | `live.py`, `collector.py` | `ts` (Unix s), `hr` bpm, `rr_ms` (space-separated RR intervals in ms) | Today, while the strap streams |
| `ble_raw` | `collector.py` drain | Every frame from the strap, `ts`, `channel`, `ptype`, `hex`. Lossless | Each drain |
| `realtime_hr` | `decode.py` | HR + RR decoded from REALTIME frames, keyed by device RTC | Each decode |
| `device_soc` | `decode.py` | Battery % from firmware "SOC report" lines, keyed by RTC | Each decode |
| `device_logs` | `decode.py` | Reassembled firmware console lines (`subsystem`, `message`) | Each decode |
| `strap_samples` | `decode.py` | Historical event records (#3 paired sensor, #63 raw optical) as raw int16 arrays. Field meanings not verified | Each decode |
| `api_recovery` | `whoop_api.py sync` | Recovery %, resting HR, HRV (RMSSD ms), SpO₂, skin temp, per wake-up | Whatever WHOOP has scored |
| `api_sleep` | `whoop_api.py sync` | Sleep start/end, stage totals (ms), performance, efficiency, consistency, respiratory rate, naps flagged | Same |
| `api_cycle` | `whoop_api.py sync` | Day strain, average/max HR, kilojoules. The open cycle has `end = NULL` | Same |
| `api_workout` | `whoop_api.py sync` | Sport, strain, HR, kJ, distance, `zone_raw` (JSON of ms per HR zone) | Same |
| `api_profile` | `whoop_api.py sync` | Key/value profile fields (first name used by the dashboard) | Same |
| `sync_state` | several | `last_ble_sync`, `api_since` (API watermark), `live_hr_now` (JSON marker for the live view) | Live |

## Clocks
- `ble_hr.ts` and `ble_raw.ts` are Unix seconds written by the Mac.
- `rtc` columns are the strap's own counter, not Unix time. If the firmware logs "RTC
  timestamp invalid", the strap clock was never set (SetClock `0x0A`).
- API timestamps are ISO-8601 UTC strings.

## "Today" vs "Archive"
- **Today** = `ble_hr` rows since local midnight. The dashboard API returns them as
  `today_hr`.
- **Archive** = everything in `api_*`. It only has what WHOOP's servers scored, so it can
  end months ago if the strap hasn't synced through the WHOOP phone app.
- Don't merge the two into one "latest" number. Compare today's strap values against an
  archive baseline and label it that way.

## Daytime HRV from RR
The dashboard computes RMSSD from today's `rr_ms`: successive differences between RR
intervals from samples less than 3.5 s apart, intervals between 280 and 2000 ms, jumps over
220 ms dropped as artefacts, at least 10 differences. Daytime RMSSD reads lower than
WHOOP's overnight HRV; label it as daytime.

## Querying
```bash
sqlite3 -readonly whoop.db "select datetime(ts,'unixepoch','localtime'), hr, rr_ms from ble_hr order by ts desc limit 10"
sqlite3 -readonly whoop.db "select created_at, recovery_score, hrv_rmssd_ms from api_recovery order by created_at desc limit 7"
```
