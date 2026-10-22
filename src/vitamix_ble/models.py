"""Dataclasses for the identified device and its polled state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .containers import Container


class PanelType(StrEnum):
    """Control-panel generation, as the official app detects it."""

    C = "c"
    """Ascent A3300/A3500: registers on slaves 2, 3 and 4."""

    B = "b"
    """Ascent A2300/A2500, Venturist: registers on slave 0x67."""

    OLD_B = "old_b"
    """Early B-panel (model word 0): no live telemetry, byte-packed programs."""


@dataclass(frozen=True)
class DeviceInfo:
    """What ``VitamixClient.identify`` learned about the panel."""

    panel: PanelType
    model: str | None
    """Marketing model, or ``None`` when the app would not recognise it."""

    model_code: int
    """Raw model word (C: ``2/0x081a``, B: ``0x67/0x3482``)."""

    firmware: dict[str, tuple[int, ...]]
    """Raw firmware words per board (C only); missing when a read failed."""

    ble_power: tuple[int, ...] | None
    """Raw BLE power register words, or ``None`` when unreadable."""

    @property
    def thread_1_4(self) -> bool:
        """C Thread firmware 1.4, which reports recipe state and disables cancel."""
        return self.firmware.get("thread", ())[:2] == (1, 4)

    @property
    def supported(self) -> bool:
        """False for B model words the official app does not recognise."""
        return self.panel is PanelType.OLD_B or self.model is not None

    @property
    def has_telemetry(self) -> bool:
        """Whether sleep, container and motor state can be polled."""
        return self.supported and self.panel is not PanelType.OLD_B


class BlenderState(StrEnum):
    """Summary state, following the official app's UI states."""

    SLEEPING = "sleeping"
    """Panel asleep; turn the dial to wake it."""

    NO_CONTAINER = "no_container"
    IDLE = "idle"
    """Awake with a container, motor off, and no ready program reported.

    C-panels without Thread 1.4 firmware never report program readiness, so
    they show ``IDLE`` even with a program loaded.
    """

    PRESS_PULSE = "press_pulse"
    """C Thread 1.4: program loaded, press pulse."""

    READY = "ready"
    """Program loaded, press start (B: turn the dial to 1 first)."""

    RUNNING = "running"
    UNKNOWN = "unknown"
    """No telemetry (old B)."""


@dataclass(frozen=True)
class VitamixState:
    """A point-in-time snapshot of the blender.

    Fields the official app would not poll in the current state are ``None``:
    a sleeping panel reports nothing else, and no container means no
    container id or recipe state. The motor is reported off in both cases
    because the panel cannot run then.
    """

    state: BlenderState
    sleeping: bool | None
    container_present: bool | None
    container_id: int | None
    """Raw Self-Detect id, or ``None`` when absent or not yet stable."""

    container: Container | None
    motor_running: bool | None
    recipe_state: int | None
    """Raw recipe state (B: ``0x0200``; C Thread 1.4: ``2/0x3220``)."""

    @classmethod
    def unknown(cls) -> VitamixState:
        return cls(BlenderState.UNKNOWN, None, None, None, None, None, None)
