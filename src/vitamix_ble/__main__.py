"""Read-only probe: ``python -m vitamix_ble <address> [--raw]``.

Prints the identified panel and polled state. ``--raw`` also dumps
registers whose meaning is unverified, to help map them on real hardware.
"""

from __future__ import annotations

import argparse
import asyncio

from .client import VitamixClient, VitamixError
from .const import SLAVE_B, SLAVE_C_NORDIC, SLAVE_C_NXP, SLAVE_C_SAMD
from .models import PanelType

# (slave, register, count). Slave 0x67 answers on at least some C-panels too.
_RAW_SHARED = [(SLAVE_B, 0x0100, 5), (SLAVE_B, 0x010A, 2), (SLAVE_B, 0x0111, 3)]
_RAW_SHARED += [(SLAVE_B, 0x0200, 1), (SLAVE_B, 0x347F, 1), (SLAVE_B, 0x3480, 4)]
_RAW_C = [
    (SLAVE_C_NORDIC, 0x0100, 5),
    (SLAVE_C_NORDIC, 0x347F, 1),
    (SLAVE_C_NXP, 0x0F00, 1),
    (SLAVE_C_NXP, 0x3640, 1),
    (SLAVE_C_SAMD, 0x3220, 4),
]


async def _probe(address: str, raw: bool) -> None:
    client = await VitamixClient.from_address(address)
    async with client as vmx:
        info = await vmx.identify()
        print(info)
        print(await vmx.read_state())
        if not raw:
            return
        reads = _RAW_SHARED + (_RAW_C if info.panel is PanelType.C else [])
        for slave, register, count in reads:
            try:
                result: object = await vmx.read_registers(register, count, slave=slave)
            except VitamixError as err:
                result = err
            print(f"{slave:#04x}/{register:#06x} x{count}: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m vitamix_ble", description=__doc__)
    parser.add_argument("address", help="Bluetooth address of the blender")
    parser.add_argument("--raw", action="store_true", help="also dump unverified registers")
    args = parser.parse_args()
    asyncio.run(_probe(args.address, args.raw))


if __name__ == "__main__":
    main()
