"""Program encoding and upload sequences, following the official app.

The app's ``translateProgram`` turns up to four steps of normalized power,
duration and ramp into a fixed word vector, and ``setBlenderProgram``
writes it with a panel-specific sequence. After an upload the user still
has to press start on the blender; nothing here starts the motor.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .const import SLAVE_B, SLAVE_C_NORDIC, SLAVE_C_SAMD
from .models import PanelType

# Normalized power 0, 0.1, ..., 1 -> RPM; values in between interpolate.
RPM_TABLE: Final = (0, 1400, 1687, 2362, 3662, 4500, 6075, 8325, 11475, 15750, 22500)
MAX_RPM: Final = 22500
PROGRAM_SEGMENTS: Final = 4
MAX_SEGMENT_SECONDS: Final = 240
RAMP_TICK_SECONDS: Final = 0.0625
_PAD_C_B: Final = (2000, 0, 1)
_PAD_OLD_B: Final = (100, 0, 1)

# Upload registers per panel.
REG_C_PROGRAM_TAIL: Final = 0x020C  # NORDIC: words 11..13
REG_C_PROGRAM_HEAD: Final = 0x3202  # SAMD: words 0..6
REG_C_PROGRAM_MID: Final = 0x3209  # SAMD: words 7..10 then 8..10 again (as the app does)
REG_C_PROGRAM_CONTROL: Final = 0x3201  # SAMD: 1 = program ready, 2 = cancel
REG_B_PROGRAM: Final = 0x0201  # one word per register
REG_B_CONTAINER_MASK: Final = 0x3483
REG_B_RECIPE: Final = 0x0200  # 3 = program ready, 0 = cancel
REG_OLD_B_PROGRAM: Final = 0x3406
REG_OLD_B_CONTAINER_MASK: Final = 0x3405
REG_OLD_B_CONTROL: Final = 0x3404  # 1 = program ready, 0 = cancel

Request = tuple[int, int, list[int]]  # (slave, register, words)


@dataclass(frozen=True)
class ProgramStep:
    """One step of a custom program."""

    power: float
    """Normalized motor power, 0..1 (0.5 is about 4500 RPM)."""

    seconds: float
    """Duration; whole seconds are used and steps under 1 s are skipped."""

    ramp_seconds: float = 1.0
    """Time to reach this step's speed, at least 0.0625 s."""


def _f32(value: float) -> float:
    """Round to float32, matching the app's arithmetic."""
    result: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return result


def _rpm(power: float) -> float:
    x = _f32(_f32(power) * 10.0)
    index = math.floor(x)
    if index >= 10:
        return float(MAX_RPM)
    low, high = RPM_TABLE[index], RPM_TABLE[index + 1]
    return _f32(_f32((high - low) * _f32(x - index)) + low)


def program_seconds(steps: Sequence[ProgramStep]) -> int:
    """Total run time in the whole seconds that will be uploaded."""
    return sum(int(step.seconds) for step in steps if step.seconds >= 1)


def encode_program(steps: Sequence[ProgramStep], panel: PanelType) -> list[int]:
    """Encode ``steps`` into the word vector the app uploads.

    C and B: ``[minutes, seconds]`` then four ``(RPM, seconds, ramp)``
    triples. Old B: four ``(speed, seconds, ramp)`` triples with speed on a
    0..200 scale, packed two bytes per word.

    Raises:
        ValueError: for inputs outside the app's normal domain, or a program
            that would need more than four segments (steps over 240 s split).
    """
    if not steps:
        raise ValueError("a program needs at least one step")
    segments: list[tuple[int, int, int]] = []
    previous = 0.0
    for index, step in enumerate(steps):
        if not (math.isfinite(step.power) and 0 <= step.power <= 1):
            raise ValueError(f"step {index}: power must be 0..1 (got {step.power})")
        if not (math.isfinite(step.seconds) and step.seconds >= 0):
            raise ValueError(f"step {index}: seconds must be >= 0 (got {step.seconds})")
        if not (math.isfinite(step.ramp_seconds) and step.ramp_seconds >= RAMP_TICK_SECONDS):
            raise ValueError(f"step {index}: ramp must be >= {RAMP_TICK_SECONDS} s")
        rpm = _rpm(step.power)
        ticks = int(step.ramp_seconds / RAMP_TICK_SECONDS)
        rate = _f32(_f32(abs(_f32(previous - rpm)) * 200.0) / ticks)
        ramp = max(1, min(5, math.floor(rate + 0.5)))
        speed = int(_f32(_f32(rpm / MAX_RPM) * 200.0)) if panel is PanelType.OLD_B else int(rpm)
        remaining = int(step.seconds)
        # Check before expanding, so a huge duration fails fast.
        needed = len(segments) + -(-remaining // MAX_SEGMENT_SECONDS)
        if needed > PROGRAM_SEGMENTS:
            raise ValueError(
                f"program needs at least {needed} segments; at most {PROGRAM_SEGMENTS} fit"
                f" (steps over {MAX_SEGMENT_SECONDS} s count as several)"
            )
        while remaining > 0:
            segments.append((speed, min(remaining, MAX_SEGMENT_SECONDS), ramp))
            remaining -= MAX_SEGMENT_SECONDS
        previous = rpm
    if not segments:
        raise ValueError("every step is shorter than 1 second")
    pad = _PAD_OLD_B if panel is PanelType.OLD_B else _PAD_C_B
    segments += [pad] * (PROGRAM_SEGMENTS - len(segments))
    flat = [value for segment in segments for value in segment]
    if panel is PanelType.OLD_B:
        return [(flat[i] | flat[i + 1] << 8) & 0xFFFF for i in range(0, len(flat), 2)]
    total = program_seconds(steps)
    header = [0, 60] if total == 60 else [total // 60, total % 60]
    return header + flat


def container_mask(container_id: int) -> int:
    """Bitmask B and old-B panels expect for the attached container."""
    return (1 << ((container_id - 1) & 31)) & 0xFFFF


def upload_requests(panel: PanelType, words: list[int], container_id: int) -> list[Request]:
    """The ordered writes ``setBlenderProgram`` sends for ``panel``."""
    if panel is PanelType.C:
        return [
            (SLAVE_C_NORDIC, REG_C_PROGRAM_TAIL, words[11:]),
            (SLAVE_C_SAMD, REG_C_PROGRAM_HEAD, words[:7]),
            (SLAVE_C_SAMD, REG_C_PROGRAM_MID, words[7:11] + words[8:11]),
            (SLAVE_C_SAMD, REG_C_PROGRAM_CONTROL, [1]),
        ]
    if panel is PanelType.B:
        base, mask_register, control, ready = (
            REG_B_PROGRAM,
            REG_B_CONTAINER_MASK,
            REG_B_RECIPE,
            3,
        )
    else:
        base, mask_register, control, ready = (
            REG_OLD_B_PROGRAM,
            REG_OLD_B_CONTAINER_MASK,
            REG_OLD_B_CONTROL,
            1,
        )
    return [
        *((SLAVE_B, base + i, [word]) for i, word in enumerate(words)),
        (SLAVE_B, mask_register, [container_mask(container_id)]),
        (SLAVE_B, control, [ready]),
    ]


def cancel_request(panel: PanelType) -> Request:
    """The write ``cancelCurrentProgram`` sends for ``panel``."""
    if panel is PanelType.C:
        return (SLAVE_C_SAMD, REG_C_PROGRAM_CONTROL, [2])
    if panel is PanelType.B:
        return (SLAVE_B, REG_B_RECIPE, [0])
    return (SLAVE_B, REG_OLD_B_CONTROL, [0])
