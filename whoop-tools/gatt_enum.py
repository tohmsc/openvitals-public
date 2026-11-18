#!/usr/bin/env python3
"""Connect to a BLE device and dump the full GATT table + read everything readable."""
import asyncio, sys
from bleak import BleakClient, BleakScanner

ADDR = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: gatt_enum.py <BLE address>  (find it with ble_scan.py)")

# Known SIG short names
NAMES = {
    "2a00":"DeviceName","2a01":"Appearance","2a19":"BatteryLevel","2a23":"SystemID",
    "2a24":"ModelNumber","2a25":"SerialNumber","2a26":"FirmwareRev","2a27":"HardwareRev",
    "2a28":"SoftwareRev","2a29":"ManufacturerName","2a37":"HeartRateMeasurement",
    "2a38":"BodySensorLocation","2a50":"PnPID","180d":"HeartRateService",
    "180a":"DeviceInfo","180f":"Battery","1800":"GAP","1801":"GATT",
}
def nm(u):
    s = u.lower()
    short = s[4:8] if s.startswith("0000") else None
    return NAMES.get(short, "")

def pr(b):
    try: return b.decode("utf-8", "replace")
    except: return ""

async def main():
    print(f"connecting to {ADDR} ...")
    async with BleakClient(ADDR, timeout=20.0) as c:
        print("connected:", c.is_connected, "\n")
        for s in c.services:
            print(f"SERVICE {s.uuid}  {nm(s.uuid)}")
            for ch in s.characteristics:
                props = ",".join(ch.properties)
                label = nm(ch.uuid)
                line = f"  CHAR {ch.uuid} [{props}] {label}"
                if "read" in ch.properties:
                    try:
                        v = await c.read_gatt_char(ch)
                        line += f"\n       = hex:{v.hex(' ')}  ascii:{pr(v)!r}"
                    except Exception as e:
                        line += f"\n       (read err: {e})"
                print(line)
                for d in ch.descriptors:
                    print(f"       descr {d.uuid}")
        print("\n--- subscribing to all notify/indicate chars for 15s ---")
        seen = {}
        def mk(uuid):
            def cb(_, data):
                seen.setdefault(uuid, 0)
                seen[uuid] += 1
                if seen[uuid] <= 6:
                    print(f"NOTIFY {uuid}: {data.hex(' ')}  {pr(data)!r}")
            return cb
        subbed = []
        for s in c.services:
            for ch in s.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await c.start_notify(ch, mk(ch.uuid)); subbed.append(ch.uuid)
                    except Exception as e:
                        print(f"  sub fail {ch.uuid}: {e}")
        print("subscribed:", len(subbed))
        await asyncio.sleep(15)
        print("\nnotify counts:", seen)

asyncio.run(main())
