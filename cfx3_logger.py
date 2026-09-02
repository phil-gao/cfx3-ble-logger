#!/usr/bin/env python3
"""Standalone Dometic CFX3 BLE logger for Linux (Raspberry Pi / BlueZ).

Connects to a bonded CFX3 cooler, performs the DDM1 handshake
(PING -> ACK -> HELLO -> ACK), subscribes to the app's dashboard topics, and
logs fridge state (temperature, setpoint, cooling, door, input voltage,
errors/alerts) to a latest-snapshot JSON plus a daily JSONL history file.

Protocol modules in dometic/ are from philippe-a11y's Home Assistant
integration (MIT) — see THIRD_PARTY_LICENSES.

Usage:
    1. Copy config.example.json to config.json, set your cooler's MAC.
       (Find it with:  python3 discover.py)
    2. Put the cooler in Bluetooth pairing mode and bond it once:
       bluetoothctl -> agent NoInputNoOutput -> default-agent
                    -> scan on ... scan off -> pair <MAC> -> trust <MAC>
    3. Run:  python3 cfx3_logger.py
       (or install cfx3-logger.service for boot persistence)
"""
import asyncio
import json
import pathlib
import subprocess
import time

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from dometic import protocol_ddm1 as p1
from dometic.protocol import CFXState

HERE = pathlib.Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
MAC = CFG["mac"].upper()
ADAPTER = CFG.get("adapter", "hci0")
OUT = pathlib.Path(CFG.get("output_dir", "/var/lib/cfx3"))
LATEST_EVERY_S = 20
HISTORY_EVERY_S = 60
KEEPALIVE_S = 120

WRITE = "537a0301-0995-481f-926c-1604e23fd515"
NOTIFY = "537a0302-0995-481f-926c-1604e23fd515"

# A bonded CFX3 barely advertises, so bleak's scan-based discovery misses it.
# Hand bleak the BlueZ D-Bus object path directly instead of discovering.
DBUS_PATH = f"/org/bluez/{ADAPTER}/dev_{MAC.replace(':', '_')}"
DEVICE = BLEDevice(MAC, "CFX3", {"path": DBUS_PATH, "props": {}})


def state_dict(s: CFXState) -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": s.sku,
        "serial": s.serial_number,
        "temp_c": (s.measured_temperature or [None])[0],
        "temp2_c": (s.measured_temperature or [None, None])[1]
        if len(s.measured_temperature or []) > 1 else None,
        "set_c": (s.set_temperature or [None])[0],
        "cooling_on": s.cooler_power,
        "compartment_on": (s.compartment_power or [None])[0],
        "door_open": (s.door_open or [None])[0],
        "input_v": s.voltage,
        "power_source": s.power_source,
        "battery_protection": s.battery_protection,
        "errors": list(s.legacy_errors or ()),
        "alerts": list(s.alerts or ()),
    }


def bluez_preconnect() -> None:
    """Open the link via BlueZ's direct connect (works for bonded devices
    that aren't advertising loudly enough for a scan to see)."""
    subprocess.run(
        ["bluetoothctl", "--timeout", "20", "connect", MAC],
        capture_output=True, text=True, timeout=30, check=False,
    )


async def run_once() -> None:
    state = CFXState()
    stage = "ping"
    ready = asyncio.Event()
    q: asyncio.Queue[bytes] = asyncio.Queue()

    await asyncio.to_thread(bluez_preconnect)
    async with BleakClient(DEVICE, timeout=45.0) as c:
        async def w(data: bytes) -> None:
            await c.write_gatt_char(WRITE, data, response=True)

        def cb(_, data: bytearray) -> None:
            q.put_nowait(bytes(data))

        await c.start_notify(NOTIFY, cb)
        await w(bytes((p1.Action.PING,)))
        last_latest = last_hist = 0.0
        while True:
            try:
                frame = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                await w(bytes((p1.Action.PING,)))   # dead link raises -> reconnect
                continue
            if not frame:
                continue
            act = frame[0]
            if act == p1.Action.ACK and not ready.is_set():
                if stage == "ping":
                    stage = "hello"
                    await w(bytes((p1.Action.HELLO,)))
                elif stage == "hello":
                    stage = "ready"
                    ready.set()
                    for topic in p1.SUBSCRIPTIONS:
                        await w(p1.subscribe_frame(topic))
            elif act == p1.Action.PING:
                await w(bytes((p1.Action.ACK,)))
            elif act == p1.Action.PUBLISH:
                p1.parse_publish(frame, state)
                await w(bytes((p1.Action.ACK,)))
                now = time.time()
                if now - last_latest > LATEST_EVERY_S:
                    OUT.mkdir(parents=True, exist_ok=True)
                    (OUT / "cfx-latest.json").write_text(
                        json.dumps(state_dict(state)))
                    last_latest = now
                if now - last_hist > HISTORY_EVERY_S:
                    day = time.strftime("%Y%m%d")
                    with open(OUT / f"cfx-{day}.jsonl", "a") as f:
                        f.write(json.dumps(state_dict(state)) + "\n")
                    last_hist = now


async def main() -> None:
    backoff = 5
    while True:
        try:
            await run_once()
        except Exception as ex:  # noqa: BLE001 — log and retry forever
            print(f"link lost/failed: {ex!r}; retry in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        else:
            backoff = 5


if __name__ == "__main__":
    asyncio.run(main())
