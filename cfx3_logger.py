#!/usr/bin/env python3
"""Standalone Dometic CFX3 BLE logger for Linux (Raspberry Pi / BlueZ).

Connects to a bonded CFX3 cooler, performs the DDM1 handshake
(PING -> ACK -> HELLO -> ACK), subscribes to the app's dashboard topics, and
logs fridge state (temperature, setpoint, cooling, door, input voltage,
errors/alerts) to a latest-snapshot JSON plus a daily JSONL history file.

Two transports (config: "transport"):
  - "bleak"    : BlueZ's high-level D-Bus GATT (bleak). Clean, pip-only.
  - "gatttool" : RAW ATT via gatttool (driven by pygatt). Bypasses BlueZ's
                 GATT layer entirely -- the robust path when BlueZ stops
                 resolving the fridge's services (see README "When bleak
                 returns zero services").
  - "auto"     : try bleak; if BlueZ never resolves the services /
                 characteristic, fall back to gatttool for good. (default)

Protocol modules in dometic/ are from philippe-a11y's Home Assistant
integration (MIT) -- see THIRD_PARTY_LICENSES.

Usage:
    1. Copy config.example.json to config.json, set your cooler's MAC.
       (Find it with:  python3 discover.py)
    2. Put the cooler in Bluetooth pairing mode and bond it once:
       bluetoothctl -> agent NoInputNoOutput -> default-agent
                    -> scan on ... scan off -> pair <MAC> -> trust <MAC>
    3. Run:  python3 cfx3_logger.py
       (or install cfx3-logger.service for boot persistence)

The "gatttool" transport needs the `gatttool` binary (bluez package) plus
`pip install pygatt pexpect`.
"""
import json
import pathlib
import queue
import subprocess
import time

from dometic import protocol_ddm1 as p1
from dometic.protocol import CFXState

HERE = pathlib.Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
MAC = CFG["mac"].upper()
ADAPTER = CFG.get("adapter", "hci0")
OUT = pathlib.Path(CFG.get("output_dir", "/var/lib/cfx3"))
TRANSPORT = CFG.get("transport", "auto").lower()   # auto | bleak | gatttool
HISTORY_EVERY_S = 60
KEEPALIVE_S = 120

WRITE = "537a0301-0995-481f-926c-1604e23fd515"
NOTIFY = "537a0302-0995-481f-926c-1604e23fd515"

# A bonded CFX3 barely advertises, so bleak's scan-based discovery misses it.
# Hand bleak the BlueZ D-Bus object path directly instead of discovering.
DBUS_PATH = f"/org/bluez/{ADAPTER}/dev_{MAC.replace(':', '_')}"


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


class Session:
    """Transport-agnostic DDM1 state machine. A transport feeds it received
    notification frames (``feed``) and supplies a ``send`` callable."""

    def __init__(self) -> None:
        self.state = CFXState()
        self.stage = "ping"
        self.ready = False
        self._last_hist = 0.0

    def handshake(self, send) -> None:
        send(bytes((p1.Action.PING,)))

    def feed(self, frame: bytes, send) -> None:
        if not frame:
            return
        act = frame[0]
        if act == p1.Action.ACK and not self.ready:
            if self.stage == "ping":
                self.stage = "hello"
                send(bytes((p1.Action.HELLO,)))
            elif self.stage == "hello":
                self.stage = "ready"
                self.ready = True
                for topic in p1.SUBSCRIPTIONS:
                    send(p1.subscribe_frame(topic))
        elif act == p1.Action.PING:
            send(bytes((p1.Action.ACK,)))
        elif act == p1.Action.PUBLISH:
            p1.parse_publish(frame, self.state)
            send(bytes((p1.Action.ACK,)))
            self._emit()

    def _emit(self) -> None:
        # The fridge dumps its state in a burst right after subscribe; a
        # throttle here drops those values and leaves the snapshot stuck on the
        # first (empty) frame. The file is tiny -- just write every frame.
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "cfx-latest.json").write_text(json.dumps(state_dict(self.state)))
        now = time.time()
        if now - self._last_hist > HISTORY_EVERY_S:
            with open(OUT / f"cfx-{time.strftime('%Y%m%d')}.jsonl", "a") as f:
                f.write(json.dumps(state_dict(self.state)) + "\n")
            self._last_hist = now


def _bluez_preconnect() -> None:
    """Open the link via BlueZ's direct connect (works for bonded devices
    that aren't advertising loudly enough for a scan to see)."""
    subprocess.run(
        ["bluetoothctl", "--timeout", "20", "connect", MAC],
        capture_output=True, text=True, timeout=30, check=False,
    )


# ---------------------------------------------------------------- bleak (BlueZ)
class _NoServices(Exception):
    """BlueZ connected but never resolved the fridge's GATT services."""


def run_bleak() -> None:
    import asyncio
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice
    from bleak.exc import BleakError

    device = BLEDevice(MAC, "CFX3", {"path": DBUS_PATH, "props": {}})
    sess = Session()

    async def _run() -> None:
        await asyncio.to_thread(_bluez_preconnect)
        async with BleakClient(device, timeout=45.0) as c:
            loop = asyncio.get_running_loop()
            q: "asyncio.Queue[bytes]" = asyncio.Queue()

            def send(data: bytes) -> None:
                asyncio.run_coroutine_threadsafe(
                    c.write_gatt_char(WRITE, data, response=True), loop)

            try:
                await c.start_notify(NOTIFY, lambda _h, d: q.put_nowait(bytes(d)))
            except BleakError as ex:
                # classic symptom: link up, services never resolved
                raise _NoServices(str(ex)) from ex
            sess.handshake(lambda b: asyncio.run_coroutine_threadsafe(
                c.write_gatt_char(WRITE, b, response=True), loop))
            while True:
                try:
                    frame = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    await c.write_gatt_char(WRITE, bytes((p1.Action.PING,)), response=True)
                    continue
                sess.feed(frame, send)

    asyncio.run(_run())


# --------------------------------------------------------- gatttool (raw ATT)
def run_gatttool() -> None:
    """Raw-ATT transport via pygatt/gatttool -- bypasses BlueZ's GATT layer."""
    import logging
    import pygatt
    logging.getLogger("pygatt").setLevel(logging.CRITICAL)

    sess = Session()
    q: "queue.Queue[bytes]" = queue.Queue()
    # free the single BLE slot from bluetoothd before gatttool grabs it
    subprocess.run(["bluetoothctl", "--timeout", "6", "disconnect", MAC],
                   capture_output=True, timeout=12, check=False)
    adapter = pygatt.GATTToolBackend(hci_device=ADAPTER)
    # reset_on_start resets the whole adapter -> would kill any passive scanner
    # (e.g. a Victron logger) sharing it. Never reset here.
    adapter.start(reset_on_start=False)
    try:
        dev = adapter.connect(MAC, timeout=25)
        dev.subscribe(NOTIFY, callback=lambda _h, v: q.put(bytes(v)))

        def send(b: bytes) -> None:
            dev.char_write(WRITE, bytearray(b), wait_for_response=True)

        sess.handshake(send)
        idle = 0
        while True:
            try:
                frame = q.get(timeout=30)
            except queue.Empty:
                idle += 1
                if idle >= 3:                     # ~90s of silence -> reconnect
                    raise RuntimeError("no frames; reconnecting")
                send(bytes((p1.Action.PING,)))    # keepalive; dead link raises
                continue
            idle = 0
            sess.feed(frame, send)
    finally:
        try:
            adapter.stop()
        except Exception:
            pass


def main() -> None:
    mode = TRANSPORT if TRANSPORT in ("bleak", "gatttool") else "bleak"
    auto = TRANSPORT == "auto"
    backoff = 5
    while True:
        try:
            (run_gatttool if mode == "gatttool" else run_bleak)()
        except _NoServices as ex:
            if auto and mode == "bleak":
                mode = "gatttool"
                print(f"bleak: BlueZ resolved no services ({ex}); "
                      f"switching to raw-ATT (gatttool) transport", flush=True)
                continue
            print(f"link lost/failed: {ex!r}; retry in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except Exception as ex:  # noqa: BLE001 -- log and retry forever
            print(f"link lost/failed: {ex!r}; retry in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        else:
            backoff = 5


if __name__ == "__main__":
    main()
