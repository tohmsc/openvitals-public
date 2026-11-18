import asyncio
from bleak import BleakScanner

async def main():
    print("scanning 8s...")
    devs = await BleakScanner.discover(timeout=8.0, return_adv=True)
    if not devs:
        print("no devices (could be permissions)")
    for addr,(d,adv) in sorted(devs.items(), key=lambda x:-(x[1][1].rssi or -999)):
        name = d.name or adv.local_name or "?"
        svcs = ",".join(adv.service_uuids) if adv.service_uuids else ""
        mfg = {k:v.hex() for k,v in (adv.manufacturer_data or {}).items()}
        print(f"{adv.rssi:>4} {addr}  name={name!r}  svcs=[{svcs}]  mfg={mfg}")

asyncio.run(main())
