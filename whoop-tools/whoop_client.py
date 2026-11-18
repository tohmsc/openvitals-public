#!/usr/bin/env python3
"""Minimal, self-contained WHOOP 4.0 live client.

Connects to the strap, enables heart-rate broadcast via the custom command
channel, parses the standard BLE Heart-Rate Measurement (HR + RR intervals),
and logs everything to CSV + a raw frame log you can replay for decoding.

Usage: whoop_client.py [BLE_ADDRESS] [SECONDS]
"""
import asyncio, sys, time, csv, datetime as dt
from bleak import BleakClient, BleakScanner

ADDR = sys.argv[1] if len(sys.argv) > 1 else None
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

WHOOP_SVC = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
BASE = "6108{:04x}-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_CHAR  = BASE.format(2)
NOTIFY    = [BASE.format(n) for n in (3, 4, 5, 7)]
HR_CHAR   = "00002a37-0000-1000-8000-00805f9b34fb"
HR_ON     = bytes.fromhex("aa0800a823080e016c935474")  # community "broadcast HR on"

def parse_hr(data: bytes):
    """Return (hr_bpm, [rr_ms,...]) from a 0x2A37 Heart Rate Measurement."""
    flags = data[0]
    i = 1
    if flags & 0x01:                      # 16-bit HR
        hr = int.from_bytes(data[i:i+2], "little"); i += 2
    else:
        hr = data[i]; i += 1
    if flags & 0x08:                      # energy expended
        i += 2
    rr = []
    if flags & 0x10:                      # RR intervals, units 1/1024 s
        while i + 1 < len(data):
            rr.append(round(int.from_bytes(data[i:i+2], "little") / 1024 * 1000, 1))
            i += 2
    return hr, rr

async def find_whoop():
    print("scanning for WHOOP (custom service)...")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: WHOOP_SVC in (adv.service_uuids or []), timeout=15.0)
    if dev:
        print("found:", dev.address)
    return dev.address if dev else None

async def main():
    addr = ADDR or await find_whoop()
    if not addr:
        print("WHOOP not found. Make sure the phone app is closed (one bond at a time).")
        return
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    hr_f = open(f"whoop-tools/hr_{stamp}.csv", "w", newline="")
    raw_f = open(f"whoop-tools/raw_{stamp}.log", "w")
    hr_w = csv.writer(hr_f); hr_w.writerow(["iso_time", "hr_bpm", "rr_ms"])
    t0 = time.time()

    async with BleakClient(addr, timeout=20.0) as c:
        print("connected:", c.is_connected)

        def on_hr(_, data):
            hr, rr = parse_hr(data)
            iso = dt.datetime.now().isoformat()
            hr_w.writerow([iso, hr, " ".join(map(str, rr))]); hr_f.flush()
            print(f"HR {hr:3d} bpm   RR {rr}")

        def on_raw(uuid, data):
            line = f"{time.time()-t0:8.3f} {uuid[4:8]} {data.hex(' ')}"
            raw_f.write(line + "\n"); raw_f.flush()
            print(line)

        for u in NOTIFY:
            await c.start_notify(u, lambda _, d, u=u: on_raw(u, d))
        await c.start_notify(HR_CHAR, on_hr)
        await c.write_gatt_char(CMD_CHAR, HR_ON, response=False)
        print(f"recording {DURATION:.0f}s -> hr_{stamp}.csv / raw_{stamp}.log")
        await asyncio.sleep(DURATION)

    hr_f.close(); raw_f.close()
    print("saved.")

asyncio.run(main())
