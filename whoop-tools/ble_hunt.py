import asyncio
from bleak import BleakScanner

hits = {}
def cb(d, adv):
    hits[d.address] = (adv.rssi, d.name or adv.local_name, adv.service_uuids,
                       {k:v.hex() for k,v in (adv.manufacturer_data or {}).items()},
                       {k:v.hex() for k,v in (adv.service_data or {}).items()})

async def main():
    s = BleakScanner(detection_callback=cb)
    await s.start(); await asyncio.sleep(16); await s.stop()
    print(f"{len(hits)} devices\n")
    for a,(r,n,su,md,sd) in sorted(hits.items(), key=lambda x:-(x[1][0] or -999)):
        flag = ""
        blob = (str(n)+str(su)+str(md)+str(sd)).lower()
        if "whoop" in blob or "180d" in blob or "2a37" in blob: flag=" <== HR/WHOOP?"
        print(f"{r:>4} {a} name={n!r}{flag}")
        if su: print(f"       svcs={su}")
        if md: print(f"       mfg={md}")
        if sd: print(f"       svc_data={sd}")

asyncio.run(main())
