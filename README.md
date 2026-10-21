# cfx3-ble-logger

Log your **Dometic CFX3** fridge/freezer — temperature, setpoint, cooling state,
door, input voltage, errors — from a **Linux box's own Bluetooth** (Raspberry Pi
+ BlueZ). No Home Assistant, no ESP32, no cloud, no Dometic app.

One JSON snapshot every 20 s, one JSONL history line per minute:

```json
{"ts": "2026-09-02T14:23:53-0700", "model": "CFX355IM", "temp_c": 6.0,
 "set_c": 0.0, "cooling_on": true, "door_open": false, "input_v": 13.3,
 "power_source": 1, "battery_protection": 0, "errors": [], "alerts": []}
```

I built this to feed my truck's off-grid power dashboard (the fridge is the
one load that's never allowed to die, so it earns telemetry).

## Why this exists

There was no standalone Linux path to a CFX3. The working open implementations
were an ESPHome/ESP32 component and a Home Assistant integration — great, but I
wanted a plain Pi daemon writing plain files. Getting there meant two BlueZ
traps worth documenting:

1. **The CFX3 exposes no GATT services until it's bonded** — it accepts a
   connection and shows you nothing. It also silently refuses to bond unless
   it's freshly in pairing mode. Bond once (Just Works, no PIN) and the bond
   persists.
2. **A bonded CFX3 barely advertises**, so bleak's scan-based discovery fails
   with `BleakDeviceNotFoundError` even though the fridge is right there.
   Fix: `bluetoothctl connect <MAC>` first (BlueZ connects bonded devices by
   address), and hand bleak the D-Bus object path directly via a synthetic
   `BLEDevice` — no discovery step at all.

The protocol itself is Dometic's **DDM1** (CFX3 dialect): a
PING → ACK → HELLO → ACK handshake, then per-topic subscribe frames, then the
fridge publishes state — and every publish must be ACKed or it stops talking.
Temperatures are signed little-endian deci-°C; voltage is unsigned LE deci-V.

## When bleak returns zero services (the raw-ATT transport)

On some BlueZ + fridge-firmware combinations, BlueZ's high-level GATT layer
**silently stops resolving the CFX3's services**. The link connects fine, it's
stable and even encrypted — but `bluetoothctl info <MAC>` never shows
`ServicesResolved: yes` and `list-attributes` is empty, so **bleak dies at
`start_notify` with `BleakCharacteristicNotFoundError (537a0302 … not found)`**
and retries forever. (The CFX3's BLE module is an ESP32; BlueZ's robust-caching /
"Server Supported Features" handshake appears to choke on its firmware.)

The fridge is fine — it's BlueZ's abstraction that's broken. Prove it with the
raw protocol:

```bash
sudo btmon &                  # watch the actual ATT traffic on the wire
gatttool -b <MAC> --primary   # raw ATT service discovery (bypasses BlueZ GATT)
#  -> shows service 537a0300 @ 0x0028-0xffff even when bluetoothctl shows nothing
gatttool -b <MAC> --characteristics
#  -> WRITE 537a0301 @ handle 0x002a ; NOTIFY 537a0302 @ 0x002e (CCCD 0x002f)
```

If raw ATT works but bleak doesn't, use the **`gatttool` transport**: it drives
`gatttool` via [pygatt] and speaks ATT directly, sidestepping BlueZ's GATT layer.
Set `"transport": "gatttool"` in `config.json`, or leave it `"auto"` (the
default) — the logger tries bleak first and **falls back to raw ATT
automatically** the moment BlueZ fails to resolve the services. The `gatttool`
transport needs `pip install pygatt pexpect` plus the `gatttool` binary (ships
with the bluez package).

[pygatt]: https://github.com/peplin/pygatt

## Install

On a Pi (or any Linux with BlueZ) with Python 3.11+:

```bash
git clone https://github.com/phil-gao/cfx3-ble-logger
cd cfx3-ble-logger
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

**Find your fridge's MAC** (fridge powered on, in range):

```bash
venv/bin/python discover.py
```

Put the MAC in `config.json`.

**Bond once.** Make sure the Dometic app on your phone is fully closed (the
CFX3 allows a single BLE connection — the app will steal it). On the fridge,
toggle Bluetooth off/on in its settings menu to re-arm pairing, then:

```bash
bluetoothctl
  agent NoInputNoOutput
  default-agent
  scan on          # wait until your CFX3 appears
  scan off
  pair XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX
  quit
```

`Pairing successful` = done forever (the bond survives reboots).

**Run it:**

```bash
sudo mkdir -p /var/lib/cfx3 && sudo chown $USER /var/lib/cfx3
venv/bin/python cfx3_logger.py
```

Within ~30 s, `/var/lib/cfx3/cfx-latest.json` appears and a
`cfx-YYYYMMDD.jsonl` history starts growing.

**Keep it running** — edit the paths in `cfx3-logger.service`, then:

```bash
sudo cp cfx3-logger.service /etc/systemd/system/
sudo systemctl enable --now cfx3-logger
```

## Notes & caveats

- **Close the phone app.** One BLE connection at a time; the logger
  auto-reconnects with backoff when the link frees up, but you'll have gaps.
- `cooling_on` is the compartment's power state, not a live compressor flag —
  the CFX3 doesn't expose one. Compressor duty cycle shows up clearly in the
  temperature sawtooth (and in your battery monitor if you have one).
- No watts here: the fridge doesn't measure its own power. Pair this with a
  shunt (e.g. Victron SmartShunt via
  [keshavdv/victron-ble](https://github.com/keshavdv/victron-ble)) for draw.
- **One radio can't both scan and hold a connection well.** If the same adapter
  is also running a passive BLE *scanner* (e.g. a Victron instant-readout
  logger), its continuous scanning starves this connection and the fridge link
  drops every minute or so (it auto-reconnects — you just get gaps). Duty-cycle
  the scanner (short scan windows, then idle), or give the fridge its own USB
  Bluetooth dongle. The logger survives the drops either way.
- Tested on a CFX3-55IM + Raspberry Pi 5 (Bookworm, BlueZ 5.x). Other CFX3
  models speak the same DDM1 dialect and should work; CFX2/CFX5 use DDM2 —
  the protocol module for it is included but this logger doesn't wire it up.

## Thanks

- **[philippe-a11y/home-assistant-dometic-cfx](https://github.com/philippe-a11y/home-assistant-dometic-cfx)** —
  the `dometic/` protocol modules here are lifted from this excellent MIT-licensed
  integration, which nailed the DDM1/DDM2 codecs and the handshake. This repo is
  essentially "their protocol work, minus Home Assistant."
- **[prebsit/dometic-fjx7-ha](https://github.com/prebsit/dometic-fjx7-ha)** —
  reverse-engineering notes on a sibling Dometic device that documented the
  bonding behaviour and BlueZ quirks of Dometic's BLE module.
- **[keshavdv/victron-ble](https://github.com/keshavdv/victron-ble)** — the
  tool (and the spirit) that started my whole "just log everything in the
  truck" project.

## License

MIT — see [LICENSE](LICENSE). The `dometic/` protocol modules retain their
original MIT license from philippe-a11y (see
[THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES)).
