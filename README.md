# vitamix-ble

[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/vitamix-ble.svg)](https://pypi.org/project/vitamix-ble/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Async Python library for talking to Bluetooth-enabled Vitamix blenders
(Ascent A2300 / A2500 / A3300 / A3500 and Venturist 330 / 350 — the
"Self-Detect" generation).

The library implements the proprietary register-mapped BLE protocol that
the official *Vitamix Perfect Blend* app uses, derived from clean-room
observation of the wire and offline analysis of the app's bundled native
library.

> [!IMPORTANT]
> This is an **unofficial** project. It is not endorsed by, affiliated
> with, or supported by Vita-Mix Corporation. Use at your own risk.
> Writes that arm a saved program slot can cause the blade to spin if
> the user subsequently presses Start, so keep your hands clear.

## Status

- Panel detection and state polling follow the official Perfect Blend
  app's behaviour, recovered from its native library. C-panel paths
  (slaves 2/3/4) still need confirmation on real hardware: run
  `python -m vitamix_ble <address> --raw` and share the output.
- Program upload and motor control are a work in progress.
- Tested on Linux/BlueZ via [bleak](https://github.com/hbldh/bleak) and
  Home Assistant Bluetooth proxies.

## Install

```bash
pip install vitamix-ble
```

Python 3.11+ is required.

## Quick start

```python
import asyncio
from vitamix_ble import VitamixClient


async def main() -> None:
    client = await VitamixClient.from_address("AA:BB:CC:DD:EE:FF")
    async with client as vmx:
        print(await vmx.identify())
        print(await vmx.read_state())


asyncio.run(main())
```

`identify()` runs once per client and can be skipped on later connections
by passing its result as `VitamixClient(device, device_info=info)`. Or from
a shell: `python -m vitamix_ble AA:BB:CC:DD:EE:FF`.

## Panels

The official app identifies the panel by reading `0x081a` on slave 2:

| Outcome | Panel | Registers |
|---|---|---|
| reply | **C** (Ascent A3300/A3500) | slave 2 (main), 3 (NFC), 4 (radio) |
| timeout | **B** (Ascent A2300/A2500, Venturist) | slave `0x67` |
| timeout, B model word `0` | **old B** | slave `0x67`, no live telemetry |

## What you can read

`read_state()` polls what the app polls and returns a `BlenderState`
(`sleeping`, `no_container`, `idle`, `press_pulse`, `ready`, `running`,
`unknown`) plus the raw fields:

| Field | C-panel | B-panel |
|---|---|---|
| sleeping | `4/0x347F` == 1 | `0x67/0x347F` == 1 |
| container present | `3/0x0F00` == 1 | `0x67/0x3480` word 0 == 1 |
| container id | `3/0x3640` (3 matching reads) | `0x67/0x3480` word 1 |
| motor running | `4/0x0100` word 0 == 1 | `0x67/0x0100` == 1 |
| recipe state | `2/0x3220` word 1 if word 0 == 101 (Thread 1.4 only) | `0x67/0x0200` (3 = ready) |

`read_ble_power()` reads `0x34FC`, which the app sets to 9 during setup
(units unknown). Earlier probing found plausible motor values at
`0x67/0x0101`..`0x0113` (RPM, rated watts), but the app never reads them,
so they are only shown by `--raw`.

## What you can do

The official app's commands are exposed as high-level methods. Today:

- `await vmx.cancel_program()` — sends the canonical "cancel current
  program" packet (writes `0x0000` to register `0x0200`). Idempotent on
  an idle device.
- `await vmx.read_registers(reg, count)` and `await vmx.write_registers(reg, values)`
  — low-level escape hatches.

## Wire protocol

```
read  request  : [0x02] [slave] [reg_hi] [reg_lo] [count]
read  reply    : [opaque] [status] [val0_lo] [val0_hi] ...         (LE values)
write request  : [0x01] [slave] [reg_hi] [reg_lo] [count] [val0_lo] [val0_hi] ...
write reply    : [status]                                          (0x00 = success)
```

- Register addresses are **big-endian** in requests.
- Values are **little-endian** in both directions.
- Modbus-like addressing, but no Modbus PDU and no CRC. Replies carry no
  register or transaction id, so only one request may be outstanding.
- Status `0` is success. Error replies carry only the status byte; the
  official app names codes 11-20 (wrong slave/function/register/length, an
  internal CRC failure, a base/panel timeout), and the library raises
  `VitamixRejectedError`. A complete read reply is accepted whatever byte 1
  holds, since it changes while the motor runs.
- Requests and replies fit in one default-MTU payload (20 bytes): at most
  7 registers per write and 9 per read.
- Never write `de ad be ef` to the write characteristic. The official app
  uses it to enter firmware-update mode.

## Hacking on it

```bash
git clone https://github.com/teemow/vitamix-ble
cd vitamix-ble
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
pytest
```

## Related

- [hass-vitamix](https://github.com/teemow/hass-vitamix) — Home Assistant
  custom component built on top of this library.

## License

MIT — see [LICENSE](LICENSE).
