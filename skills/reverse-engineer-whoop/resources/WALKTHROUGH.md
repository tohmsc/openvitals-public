# Walkthrough — the path from "plugged in" to "own your data"

How the original build actually went, as prompts/steps you can hand an AI agent (e.g.
Claude Code) running on the user's machine. Each step is a checkpoint, not a script.

## 0. Framing prompt (give this to the agent)
> "I own a WHOOP 4.0. Help me read my own data off it over Bluetooth and via the official
> WHOOP API, store it locally in SQLite, show it on localhost, and back it up. Be honest
> about what's on the strap vs computed in WHOOP's cloud, and don't over-claim decoded
> fields you can't verify."

## 1. See the hardware
- USB-C shows an FTDI FT230X UART (`ls /dev/cu.usbserial-*`). A passive read of the UART
  shows low-rate battery-pack telemetry, **not** a console. Note it and move to BLE.
- BLE scan. **Filter by service UUID `61080001-8d6d-82b8-614a-1c8cb0f8dcc6`**, not the
  name. (Original gotcha: the strap and the user's phone had the same custom name; naive
  name-matching connected to the phone, which returned `Apple Inc. / iPhone…`.)

## 2. Confirm it's the strap and the command path works
- Enumerate GATT → Device Info `WHOOP Inc.`, Battery `0x64`, custom service with write
  `…0002` + notify `…0003/4/5/7`.
- Write the verified "HR broadcast on" frame to `…0002`; watch for an ACK on `…0003` and
  live HR on `0x2A37`. If that works, the protocol is cracked. Build the frame library and
  run `whoop.py selftest` to prove every command byte-exact.

## 3. Live data
- Enable realtime (`0x03`); parse REALTIME_DATA (HR + RR). Wear the strap for a real pulse.
- Stream continuously into SQLite (`live.py`), show a ● LIVE badge on the dashboard.

## 4. History off the strap
- SendHistoricalData (`0x16`), ACK each batch's `trim` (`0x17`). Incremental. Store every
  frame losslessly first; decode later.

## 5. The scored metrics (API)
- The strap does NOT hold Recovery/Strain/Sleep scores — WHOOP computes them server-side.
  Create a dev app at developer.whoop.com, OAuth, and sync `/v2/*`. This is the reliable,
  clean health record (sleep stages, HRV baseline, resting HR, SpO₂, skin temp, strain).

## 6. Decode what's worth decoding
- Realtime HR/RR: verified → structured table.
- Console logs (`0x32`): reassemble → readable firmware telemetry (battery SOC, charger,
  wrist sensor, BLE stack). High value, low risk.
- Raw optical (event `0x3F`): capture losslessly; label "candidate" — full field map needs
  a controlled capture (wear it, move on cue, correlate). Don't fabricate labels.

## 7. Make it durable
- Localhost dashboard (Flask + SQLite, static front end, vendored Chart.js). The first
  version mixed WHOOP's archive scores with today's strap data, which made 17-month-old
  sleep look like last night. The fix: **Today** is strap data only, the WHOOP API history
  is an **Archive** behind a calendar, and every archive page says it isn't today.
- A full-screen **Heart** view that beats on the strap's real RR timing, with a Resync
  button that runs one collector cycle.
- Hourly launchd collector: drain → decode → API sync → encrypted local backup → optional
  Cloudflare D1 push. Always-on dashboard service. Tell the user to save `.backup_key`.

## 8. Prompts that moved the dashboard forward
> "Can you design the dashboard and the front end to make it very beautiful? And like a
> professional healthcare monitoring system that was designed by a Silicon Valley design
> engineer."

> "Make it a full screen application that includes a view that looks like this. Maybe even
> an exploded view. Show a live pulse or heart beating as well from the data you get."

> "So the only information you have should be from like the last 2 hours. Plus the
> historicals against a previous baseline, which is fine, but don't make it look as if the
> historicals are today. There should obviously be a calendar system where it's old data then."

## What NOT to do
- Don't claim a shell/root over the UART — it's an RTOS MCU, not Linux.
- Don't trust "custom CRC" blog claims — the CRCs are standard (SMBUS + ISO-HDLC).
- Don't assert raw-sensor field meanings without a controlled capture.
- Don't promise Apple Health sync without a native iOS app (HealthKit isn't on macOS).
- Don't point any of this at a device the user doesn't own.

## Subscription note
The **raw signals and your own history** can be pulled and stored without the subscription.
The **computed scores** (Recovery/Strain/Sleep) come from WHOOP's cloud via the API, which
currently requires an active membership/credentials. If the goal is "stop paying," you keep
raw HR/HRV/optical/accel locally and can compute your own metrics from them (HR/HRV and
sleep heuristics are well documented), but WHOOP's exact scored algorithms are proprietary.
