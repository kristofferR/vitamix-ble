"""Input validation for the experimental legacy motor-control helpers."""

from __future__ import annotations

import pytest

from vitamix_ble.client import VitamixClient, VitamixUnsupportedError
from vitamix_ble.const import CUSTOM_PROGRAM_MAX_STEPS, MAX_STEP_SECONDS


def experimental() -> VitamixClient:
    return VitamixClient("AA:BB:CC:DD:EE:FF", experimental=True)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.load_program(1),
        lambda c: c.set_motor_speed(5),
        lambda c: c.start_motor(),
        lambda c: c.stop_motor(),
        lambda c: c.play_melody([(5, 1)]),
        lambda c: c.upload_custom_program([(5, 1)]),
        lambda c: c.commit_custom_program(1),
    ],
)
async def test_legacy_control_requires_opt_in(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(VitamixUnsupportedError, match="experimental"):
        await call(VitamixClient("AA:BB:CC:DD:EE:FF"))


class TestLoadProgramSlotValidation:
    """``load_program`` is the only public helper with input rules."""

    async def test_zero_rejected(self) -> None:
        # Slot 0 is reserved for "no program" — callers must use
        # cancel_program() to make intent unambiguous.
        client = experimental()
        with pytest.raises(ValueError, match="cancel_program"):
            await client.load_program(0)

    async def test_negative_rejected(self) -> None:
        client = experimental()
        with pytest.raises(ValueError):
            await client.load_program(-1)


class TestCustomProgramValidation:
    """``upload_custom_program`` must reject malformed step lists."""

    async def test_empty_steps_rejected(self) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="must not be empty"):
            await client.upload_custom_program([])

    async def test_too_many_steps_rejected(self) -> None:
        client = experimental()
        steps = [(1, 1)] * (CUSTOM_PROGRAM_MAX_STEPS + 1)
        with pytest.raises(ValueError, match="at most"):
            await client.upload_custom_program(steps)

    @pytest.mark.parametrize("speed", [-1, 11, 99])
    async def test_speed_out_of_range_rejected(self, speed: int) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="speed"):
            await client.upload_custom_program([(speed, 5)])

    async def test_negative_time_rejected(self) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="time"):
            await client.upload_custom_program([(5, -1)])


class TestPlayMelodyValidation:
    async def test_empty_rejected(self) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="must not be empty"):
            await client.play_melody([])

    @pytest.mark.parametrize("speed", [-1, 11])
    async def test_speed_out_of_range(self, speed: int) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="speed"):
            await client.play_melody([(speed, 0.5)])

    async def test_negative_duration(self) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="duration"):
            await client.play_melody([(5, -0.1)])


class TestCommitCustomProgramValidation:
    @pytest.mark.parametrize("count", [0, -1, CUSTOM_PROGRAM_MAX_STEPS + 1])
    async def test_step_count_out_of_range(self, count: int) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="step_count"):
            await client.commit_custom_program(count)


class TestSetMotorSpeedValidation:
    @pytest.mark.parametrize("speed", [-1, 11])
    async def test_out_of_range(self, speed: int) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="speed"):
            await client.set_motor_speed(speed)

    @pytest.mark.parametrize("duration", [0, -5, MAX_STEP_SECONDS + 1])
    async def test_invalid_duration(self, duration: int) -> None:
        client = experimental()
        with pytest.raises(ValueError, match="duration"):
            await client.set_motor_speed(5, duration_seconds=duration)
