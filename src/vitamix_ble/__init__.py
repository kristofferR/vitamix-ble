"""Async Python library for Vitamix Bluetooth Low Energy blenders.

Speaks the proprietary register-mapped BLE protocol of Vitamix
Ascent and Venturist series blenders (e.g. A2300/A2500/A3300/A3500,
Venturist 330/350) — the ones with the "Self-Detect" containers
and the Vitamix Perfect Blend companion app.

Protocol details follow the official app's behaviour, recovered from its
native library: the panel type is detected at runtime and each panel is
polled on its own registers. Reads are non-destructive.

Quick start:

    import asyncio
    from vitamix_ble import VitamixClient

    async def main() -> None:
        client = await VitamixClient.from_address("C9:FF:B3:03:1F:DF")
        async with client as vmx:
            print(await vmx.identify())
            print(await vmx.read_state())

    asyncio.run(main())
"""

from .client import (
    VitamixClient,
    VitamixError,
    VitamixReadRejectedError,
    VitamixRejectedError,
    VitamixTimeoutError,
    VitamixUnsupportedError,
    VitamixWriteRejectedError,
)
from .const import (
    NOTIFY_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    SLAVE_B,
    SLAVE_C_NORDIC,
    SLAVE_C_NXP,
    SLAVE_C_SAMD,
    WRITE_CHARACTERISTIC_UUID,
)
from .containers import CONTAINERS, Container, lookup_container
from .models import BlenderState, DeviceInfo, PanelType, VitamixState
from .protocol import (
    PacketStatus,
    decode_read_response,
    decode_write_response,
    encode_read,
    encode_write,
)

__version__ = "0.5.0"

__all__ = [
    "CONTAINERS",
    "NOTIFY_CHARACTERISTIC_UUID",
    "SERVICE_UUID",
    "SLAVE_B",
    "SLAVE_C_NORDIC",
    "SLAVE_C_NXP",
    "SLAVE_C_SAMD",
    "WRITE_CHARACTERISTIC_UUID",
    "BlenderState",
    "Container",
    "DeviceInfo",
    "PacketStatus",
    "PanelType",
    "VitamixClient",
    "VitamixError",
    "VitamixReadRejectedError",
    "VitamixRejectedError",
    "VitamixState",
    "VitamixTimeoutError",
    "VitamixUnsupportedError",
    "VitamixWriteRejectedError",
    "decode_read_response",
    "decode_write_response",
    "encode_read",
    "encode_write",
    "lookup_container",
]
