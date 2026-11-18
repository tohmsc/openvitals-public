#!/usr/bin/env python3
"""Connect to the WHOOP 4.0, subscribe to all notify channels, send a documented
command to the write channel, and log every byte that comes back."""
import asyncio, sys, time
from bleak import BleakClient

ADDR = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: whoop_probe.py <BLE address>  (find it with ble_scan.py)")
BASE = "6108{:04x}-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD   = BASE.format(2)          # write
DATA  = [BASE.format(n) for n in (3,4,5,7)]
HR    = "00002a37-0000-1000-8000-00805f9b34fb"

# Published community frame: "Broadcast Heart Rate On"
HR_ON = bytes.fromhex("aa0800a823080e016c935474")

t0 = time.time()
def log(tag, data):
    print(f"[{time.time()-t0:6.2f}] {tag:10s} {data.hex(' ')}")

async def main():
    async with BleakClient(ADDR, timeout=20.0) as c:
        print("connected:", c.is_connected)
        for u in DATA:
            await c.start_notify(u, lambda _, d, u=u: log(u[4:8], d))
        try:
            await c.start_notify(HR, lambda _, d: log("HR-2a37", d))
        except Exception as e:
            print("HR subscribe:", e)
        print("sending HR_ON:", HR_ON.hex(" "))
        try:
            await c.write_gatt_char(CMD, HR_ON, response=False)
        except Exception as e:
            print("write err:", e)
        print("listening 30s (channel=0003 resp, 0004 event, 0005 data)...")
        await asyncio.sleep(30)
        print("done")

asyncio.run(main())
