"""A GATT stand-in that answers register reads and writes from a map."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from vitamix_ble.client import VitamixClient

Register = tuple[int, int]  # (slave, register)


class FakeBlender:
    """Replies like a panel: missing slaves stay silent, others answer.

    ``registers`` values may be a list, consumed one per read (the last value
    repeats), to model readings that change between polls.
    """

    def __init__(
        self,
        registers: dict[Register, int | list[int]],
        *,
        slaves: set[int],
        errors: dict[Register, int] | None = None,
    ) -> None:
        self.registers = registers
        self.slaves = slaves
        self.errors = errors or {}
        self.requests: list[bytes] = []
        self.is_connected = True
        self.notify: Callable[[Any, bytearray], None] | None = None
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def start_notify(self, _uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        self.notify = callback

    async def stop_notify(self, _uuid: str) -> None:
        pass

    def _value(self, key: Register) -> int:
        value = self.registers.get(key, 0)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    async def write_gatt_char(self, _uuid: str, data: bytes, *, response: bool) -> None:
        self.requests.append(bytes(data))
        fn, slave, register, count = data[0], data[1], (data[2] << 8) | data[3], data[4]
        if slave not in self.slaves:
            return
        if (slave, register) in self.errors:
            reply = bytes([fn, self.errors.pop((slave, register))])
        elif fn == 0x02:
            words = [self._value((slave, register + i)) for i in range(count)]
            reply = bytes([0x02, 0x00]) + b"".join(w.to_bytes(2, "little") for w in words)
        else:
            for i in range(count):
                self.registers[(slave, register + i)] = int.from_bytes(
                    data[5 + 2 * i : 7 + 2 * i], "little"
                )
            reply = b"\x00"
        assert self.notify is not None
        asyncio.get_running_loop().call_soon(self.notify, None, bytearray(reply))

    def reads(self) -> list[Register]:
        return [(r[1], (r[2] << 8) | r[3]) for r in self.requests if r[0] == 0x02]


def attach(blender: FakeBlender, **kwargs: Any) -> VitamixClient:
    """A client already connected to ``blender``; reconnects reuse it (see conftest)."""
    vmx = VitamixClient(blender, **kwargs)  # type: ignore[arg-type]
    vmx._client = blender  # type: ignore[assignment]
    blender.notify = vmx._on_notify  # type: ignore[assignment]
    return vmx
