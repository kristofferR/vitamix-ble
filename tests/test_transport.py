"""Request/reply sequencing against a fake GATT client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from vitamix_ble import client as client_module
from vitamix_ble.client import VitamixClient, VitamixReadRejectedError, VitamixTimeoutError


class FakeGatt:
    """Answers each write with the next scripted reply (``None`` = no reply)."""

    def __init__(self, replies: list[bytes | None]) -> None:
        self.replies = replies
        self.writes: list[bytes] = []
        self.notify: Callable[[Any, bytearray], None] | None = None
        self.is_connected = True
        self.disconnected = False
        self.fail_notify = False
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        if self.fail_notify:
            raise RuntimeError("notify setup failed")
        self.notify = callback

    async def stop_notify(self, _uuid: str) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True
        self.is_connected = False

    async def write_gatt_char(self, _uuid: str, data: bytes, *, response: bool) -> None:
        self.writes.append(bytes(data))
        reply = self.replies.pop(0)
        if reply is not None:
            assert self.notify is not None
            asyncio.get_running_loop().call_soon(self.notify, None, bytearray(reply))

    def push(self, data: bytes) -> None:
        assert self.notify is not None
        self.notify(None, bytearray(data))


def connected(replies: list[bytes | None], timeout: float = 0.05) -> tuple[VitamixClient, FakeGatt]:
    gatt = FakeGatt(replies)
    vmx = VitamixClient("AA:BB:CC:DD:EE:FF", request_timeout=timeout)
    vmx._client = gatt  # type: ignore[assignment]
    gatt.notify = vmx._on_notify  # type: ignore[assignment]
    return vmx, gatt


async def test_read_returns_reply_values() -> None:
    vmx, _ = connected([bytes.fromhex("02003412")])
    assert await vmx.read_registers(0x0100, 1) == [0x1234]


async def test_named_read_error_raises() -> None:
    vmx, _ = connected([bytes.fromhex("020d")])
    with pytest.raises(VitamixReadRejectedError) as exc_info:
        await vmx.read_registers(0x0100, 1)
    assert exc_info.value.code == 13


async def test_unsolicited_notification_is_dropped() -> None:
    vmx, gatt = connected([bytes.fromhex("02000200")])
    gatt.push(bytes.fromhex("02000100"))
    assert await vmx.read_registers(0x0100, 1) == [2]


async def test_timeout_forces_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    vmx, gatt = connected([None, bytes.fromhex("02000200")])
    monkeypatch.setattr(client_module, "BleakClient", lambda *_a, **_kw: gatt)
    with pytest.raises(VitamixTimeoutError):
        await vmx.read_registers(0x0100, 1)
    # A late reply to the timed-out request could otherwise answer the next
    # one, so the next request runs on a fresh connection.
    assert await vmx.read_registers(0x0100, 1) == [2]
    assert gatt.disconnected
    assert gatt.connects == 1


async def test_notify_setup_failure_disconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    gatt = FakeGatt([])
    gatt.fail_notify = True
    monkeypatch.setattr(client_module, "BleakClient", lambda *_a, **_kw: gatt)
    vmx = VitamixClient("AA:BB:CC:DD:EE:FF")
    with pytest.raises(RuntimeError):
        await vmx.connect()
    assert gatt.disconnected
    assert not vmx.is_connected
