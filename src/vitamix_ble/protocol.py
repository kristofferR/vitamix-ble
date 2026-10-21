"""Pure-Python encoder / decoder for the Vitamix BLE wire protocol.

Wire format
-----------

A small register protocol with Modbus-like addressing, but no Modbus PDU
and no CRC: the client adds nothing beyond the bytes below. Register
addresses are big-endian; register values are little-endian. Every packet
fits in a single GATT write or notification (no fragmentation), and only
one request is outstanding at a time because replies carry no register,
slave or transaction id.

Read request (5 bytes)::

    [0x02] [slave] [reg_hi] [reg_lo] [count]

Read reply (2 + 2*count bytes)::

    [opaque] [status] [val0_lo] [val0_hi] [val1_lo] [val1_hi] ...

The official app ignores byte 0 (observed as ``0x02``) and treats byte 1
as the status: ``0`` is success, :data:`~vitamix_ble.const.DEVICE_ERRORS`
lists the named failures.

Write request (5 + 2*count bytes)::

    [0x01] [slave] [reg_hi] [reg_lo] [count] [val0_lo] [val0_hi] ...

Write reply (1 byte)::

    [status]      0x00 = success; nonzero = failure
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .const import (
    DEVICE_ERRORS,
    FN_READ,
    FN_WRITE,
    MAX_READ_REGISTERS,
    MAX_WRITE_REGISTERS,
    SLAVE_CPANEL,
)

__all__ = [
    "PacketStatus",
    "ProtocolError",
    "ReplyStatusError",
    "decode_read_response",
    "decode_write_response",
    "describe_status",
    "encode_read",
    "encode_write",
]


_HEADER_BYTES: Final = 5


class ProtocolError(ValueError):
    """Raised when a received packet violates the wire format."""


class ReplyStatusError(ProtocolError):
    """Raised when a read reply carries one of the device's named error statuses."""

    def __init__(self, code: int) -> None:
        super().__init__(f"device error {code}: {describe_status(code)}")
        self.code = code


def describe_status(code: int) -> str:
    """Human-readable meaning of a reply status byte."""
    if code == 0:
        return "ok"
    return DEVICE_ERRORS.get(code, "unknown error")


@dataclass(frozen=True)
class PacketStatus:
    """Outcome of a single request/response exchange."""

    ok: bool
    code: int  #: raw status / exception byte
    raw: bytes  #: full response payload as received

    @property
    def is_exception(self) -> bool:
        return not self.ok


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------


def encode_read(register: int, count: int = 1, slave: int = SLAVE_CPANEL) -> bytes:
    """Build a read-registers request packet.

    Args:
        register: First register address (0..0xFFFF).
        count: Number of consecutive 16-bit registers to read.
        slave: Bus slave address (defaults to the C-panel).

    Returns:
        Raw packet bytes ready to write to the GATT write characteristic.
    """
    _check_register(register)
    _check_count(count, MAX_READ_REGISTERS)
    _check_byte(slave, "slave")
    return bytes(
        [
            FN_READ,
            slave,
            (register >> 8) & 0xFF,
            register & 0xFF,
            count,
        ]
    )


def encode_write(
    register: int,
    values: list[int] | tuple[int, ...],
    slave: int = SLAVE_CPANEL,
) -> bytes:
    """Build a write-registers request packet.

    Args:
        register: First register address (0..0xFFFF).
        values: 16-bit values to write into ``register``, ``register+1``, …
        slave: Bus slave address (defaults to the C-panel).

    Returns:
        Raw packet bytes ready to write to the GATT write characteristic.

    Raises:
        ValueError: if any value is outside ``0..0xFFFF`` or the packet
            would exceed the device MTU.
    """
    _check_register(register)
    _check_byte(slave, "slave")
    if not values:
        raise ValueError("values must contain at least one register")
    count = len(values)
    _check_count(count, MAX_WRITE_REGISTERS)

    pkt = bytearray(
        [
            FN_WRITE,
            slave,
            (register >> 8) & 0xFF,
            register & 0xFF,
            count,
        ]
    )
    for index, value in enumerate(values):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(
                f"values[{index}] = {value!r} is not a valid u16",
            )
        pkt.append(value & 0xFF)  # low byte first (little-endian)
        pkt.append((value >> 8) & 0xFF)
    return bytes(pkt)


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


def decode_read_response(data: bytes, expected_count: int) -> tuple[list[int], int]:
    """Decode a notify payload received in response to a read request.

    Args:
        data: Raw notify payload.
        expected_count: Number of u16 registers that were requested.

    Returns:
        ``(values, status)``: the decoded register values (little-endian,
        in request order) and the status byte.

    Raises:
        ReplyStatusError: if the status is one of the device's named errors.
        ProtocolError: if the payload is otherwise malformed.
    """
    if len(data) < 2:
        raise ProtocolError(f"response too short ({len(data)} bytes)")
    status = data[1]
    if status in DEVICE_ERRORS:
        raise ReplyStatusError(status)
    body = data[2:]
    if len(body) != expected_count * 2:
        raise ProtocolError(
            f"wrong length: got {len(body)} value bytes, want {expected_count * 2}"
            f" (status {status})",
        )
    # Unnamed nonzero statuses with a complete body are passed through:
    # earlier live captures on slave 0x67 reported a changing byte here while
    # the motor ran. Callers decide whether to trust the values.
    values = [body[2 * i] | (body[2 * i + 1] << 8) for i in range(expected_count)]
    return values, status


def decode_write_response(data: bytes) -> PacketStatus:
    """Decode a notify payload received in response to a write request.

    The device sends a single status byte: ``0x00`` on success, otherwise
    an error code (see :func:`describe_status`). The status is returned
    as-is so callers can distinguish success from device-side errors.
    """
    if not data:
        raise ProtocolError("empty write response")
    code = data[0]
    return PacketStatus(ok=code == 0x00, code=code, raw=bytes(data))


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _check_register(register: int) -> None:
    if not 0 <= register <= 0xFFFF:
        raise ValueError(f"register 0x{register:x} out of range")


def _check_count(count: int, maximum: int) -> None:
    if not 1 <= count <= maximum:
        raise ValueError(f"count {count} out of range (1..{maximum})")


def _check_byte(value: int, name: str) -> None:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} 0x{value:x} is not an 8-bit value")
