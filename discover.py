#!/usr/bin/env python3
"""Find your CFX3's Bluetooth MAC: scans and prints Dometic-looking devices."""
import asyncio

from bleak import BleakScanner


async def main() -> None:
    print("scanning 12s ...")
    devs = await BleakScanner.discover(timeout=12.0, return_adv=True)
    rows = []
    for _, (d, adv) in devs.items():
        name = adv.local_name or d.name or ""
        hit = any(k in name.lower() for k in ("cfx", "dometic", "cooler"))
        rows.append((hit, adv.rssi or -999, name or "?", d.address))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    for hit, rssi, name, addr in rows[:20]:
        tag = "  <-- your cooler?" if hit else ""
        print(f"rssi={rssi:4}  {name:24}  {addr}{tag}")


asyncio.run(main())
