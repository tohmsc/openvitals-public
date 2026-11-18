# The dashboard

`dashboard.py` (Flask) serves a static, offline front end from `static/` and a small JSON
API. No build step, no CDN: Chart.js is vendored as `static/chart.umd.js`.

| File | Job |
| --- | --- |
| `static/index.html` | App shell: sidebar, Heart view, Today, Archive sections, Device, calendar |
| `static/app.css` | Design tokens (light + dark), every component, responsive rules |
| `static/app.js` | Data loading, Today vs Archive state, charts, calendar, Resync |
| `static/heart.js` | The full-screen Heart view: 3D heart, beat clock, callouts, pulse strip |

## Endpoints

| Route | Returns |
| --- | --- |
| `GET /api/summary` | `today_hr` (strap samples since midnight), `archive` (`start`/`end`), `recovery`, `sleep`, `cycle`, `workout` (newest first, ~400 each), `soc`, `logs`, `sync`, `counts`, `profile`, `demo` |
| `GET /api/live` | `live` (fresh in the last 8 s), `hr`, `rr_ms`, `age`, `recent` (last 120 samples) |
| `POST /api/sync` | Starts one `collector.py --live 15` run. Needs header `X-Vitals: 1`. Refused on demo DBs |
| `GET /api/sync` | `running`, `ok`, `started`, `finished`, `log` (last 40 lines) |

Environment: `WHOOP_DB` (database path), `PORT` (default 8787), `WHOOP_HOME` (folder with
`collector.py`, `.env`, `.tokens.json`; defaults to the dashboard's folder).

## Views

- **Heart** (`#heart`, the landing view). A heart surface (Taubin's implicit heart,
  ray-solved into a 30 × 96 lat/long mesh) drawn on canvas. It beats on real timing: the
  strap's RR intervals when live, otherwise 60/HR from the last reading while that reading
  is under 15 minutes old. After that there is no current heart rate and the heart is still;
  an old number is never shown as now. Exploded mode
  splits the mesh into five slices, each carrying one callout. The pulse strip is a PQRST
  shape placed on each beat; it is synthesized and says so.
- **Today** (`#today`). Only `today_hr`. Recorded minutes, average and lowest HR, daytime
  RMSSD, each on a bar against the archive baseline (last 30 archive days, mean ± 1 SD).
  Heart rate per minute on a real time axis, so gaps stay gaps.
- **Archive** (`#overview`, `#trends`, `#sleep`, `#activity`). WHOOP API data through a
  date lens: every widget sees data up to the end of the chosen day, so "latest" means
  that day. A calendar marks each archive day by recovery zone. Every archive page carries
  an amber "Archive" label and a banner saying nothing on it is from today.
- **Device** (`#device`). Battery from firmware reports, record counts, firmware console.

## Rules the UI follows (keep them if you change it)
- Never show archive scores as today's. Today is strap data only.
- Show data age: "Strap 4 minutes ago", "Archive to Jun 22, 2025".
- Anything synthesized (pulse waveform, demo data) is labelled.
- Works offline, in light and dark, down to phone width, with reduced motion respected.

## Adapting it
- Colours and type: tokens at the top of `app.css` (`:root` and `[data-theme="light"]`).
- Add a metric: return it from `/api/summary`, then add it to `VITALS` in `app.js` (tiles)
  or `CALLOUTS` in `heart.js` (Heart view).
- Different baseline window: `archiveBaseline()` in `app.js`.
- The heart shape: `F()` in `heart.js` is the implicit surface; `LAT`, `LON`, `BANDS` set
  mesh density and exploded slices.
