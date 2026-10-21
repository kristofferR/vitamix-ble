"""Panel identification and per-panel state polling."""

from __future__ import annotations

import pytest

from vitamix_ble import client as client_module
from vitamix_ble.client import VitamixUnsupportedError
from vitamix_ble.models import BlenderState, PanelType

from .fake_device import FakeBlender, attach

C_SLAVES = {0x02, 0x03, 0x04}


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "DETECTION_TIMEOUT", 0.01)


def c_panel(**registers: int | list[int]) -> FakeBlender:
    regs: dict[tuple[int, int], int | list[int]] = {
        (0x02, 0x081A): 0,  # A3500
        (0x02, 0x0818): 1,
        (0x02, 0x0819): 4,  # Thread firmware 1.4
        (0x04, 0x34FC): 9,
        (0x03, 0x0F00): 1,  # container present
        (0x03, 0x3640): 3,  # 48 oz wet
    }
    names = {
        "sleep": (0x04, 0x347F),
        "presence": (0x03, 0x0F00),
        "container": (0x03, 0x3640),
        "motor": (0x04, 0x0100),
        "recipe_marker": (0x02, 0x3220),
        "recipe": (0x02, 0x3221),
        "thread_minor": (0x02, 0x0819),
    }
    regs.update({names[k]: v for k, v in registers.items()})
    return FakeBlender(regs, slaves=C_SLAVES)


def b_panel(model: int = 0x031D, **registers: int) -> FakeBlender:
    regs: dict[tuple[int, int], int | list[int]] = {
        (0x67, 0x3482): model,
        (0x67, 0x3480): 1,
        (0x67, 0x3481): 1,
    }
    names = {
        "sleep": (0x67, 0x347F),
        "presence": (0x67, 0x3480),
        "container": (0x67, 0x3481),
        "motor": (0x67, 0x0100),
        "recipe": (0x67, 0x0200),
    }
    regs.update({names[k]: v for k, v in registers.items()})
    return FakeBlender(regs, slaves={0x67})


async def test_identifies_c_panel() -> None:
    info = await attach(c_panel()).identify()
    assert info.panel is PanelType.C
    assert info.model == "Ascent A3500"
    assert info.thread_1_4
    assert info.firmware["thread"] == (1, 4, 0)
    assert info.ble_power == (9, 0)


async def test_c_probe_error_is_retried() -> None:
    blender = c_panel()
    blender.errors[(0x02, 0x081A)] = 14
    assert (await attach(blender).identify()).panel is PanelType.C


async def test_failed_firmware_read_is_omitted() -> None:
    blender = c_panel()
    blender.errors[(0x03, 0x0001)] = 13
    info = await attach(blender).identify()
    assert "nxp" not in info.firmware
    assert "nordic" in info.firmware


async def test_probe_timeout_means_b_panel() -> None:
    info = await attach(b_panel()).identify()
    assert info.panel is PanelType.B
    assert info.model == "Ascent A2500"


async def test_unanswered_optional_reads_are_omitted() -> None:
    blender = b_panel()
    blender.silent.add((0x67, 0x34FC))
    info = await attach(blender, request_timeout=0.01).identify()
    assert info.model == "Ascent A2500"
    assert info.ble_power is None


async def test_old_b_has_no_telemetry() -> None:
    blender = b_panel(model=0)
    vmx = attach(blender)
    assert (await vmx.identify()).panel is PanelType.OLD_B
    polled = len(blender.requests)
    assert (await vmx.read_state()).state is BlenderState.UNKNOWN
    assert len(blender.requests) == polled


async def test_unknown_b_model_is_unsupported() -> None:
    with pytest.raises(VitamixUnsupportedError):
        await attach(b_panel(model=0x0999)).read_state()


async def test_c_sleeping_polls_nothing_else() -> None:
    blender = c_panel(sleep=1)
    vmx = attach(blender)
    await vmx.identify()
    before = len(blender.requests)
    state = await vmx.read_state()
    assert state.state is BlenderState.SLEEPING
    assert state.motor_running is False
    assert state.container_id is None
    assert len(blender.requests) == before + 1


async def test_c_no_container() -> None:
    state = await attach(c_panel(presence=0)).read_state()
    assert state.state is BlenderState.NO_CONTAINER
    assert state.container_present is False


async def test_c_container_needs_three_matching_reads() -> None:
    state = await attach(c_panel(container=[3, 4, 3])).read_state()
    assert state.container_present is True
    assert state.container_id is None


async def test_c_awake_with_container() -> None:
    state = await attach(c_panel()).read_state()
    assert state.state is BlenderState.IDLE
    assert state.container is not None
    assert state.container.short_name == "48oz_wet"


@pytest.mark.parametrize(
    ("recipe", "expected"),
    [(4, BlenderState.PRESS_PULSE), (5, BlenderState.READY), (0, BlenderState.IDLE)],
)
async def test_c_thread_1_4_recipe_state(recipe: int, expected: BlenderState) -> None:
    state = await attach(c_panel(recipe_marker=101, recipe=recipe)).read_state()
    assert state.recipe_state == recipe
    assert state.state is expected


async def test_c_recipe_state_needs_marker() -> None:
    state = await attach(c_panel(recipe_marker=7, recipe=5)).read_state()
    assert state.recipe_state is None


async def test_c_without_thread_1_4_skips_recipe_poll() -> None:
    blender = c_panel(thread_minor=3)
    await attach(blender).read_state()
    assert (0x02, 0x3220) not in blender.reads()


async def test_c_motor_running() -> None:
    state = await attach(c_panel(motor=1)).read_state()
    assert state.state is BlenderState.RUNNING
    assert state.motor_running is True


async def test_b_container_is_second_word() -> None:
    state = await attach(b_panel(container=4)).read_state()
    assert state.container_id == 4
    assert state.container is not None
    assert state.container.short_name == "48oz_dry"


async def test_b_no_container() -> None:
    state = await attach(b_panel(presence=0)).read_state()
    assert state.state is BlenderState.NO_CONTAINER


async def test_b_sleep_flag_means_sleeping() -> None:
    assert (await attach(b_panel(sleep=1)).read_state()).state is BlenderState.SLEEPING


async def test_b_recipe_ready() -> None:
    state = await attach(b_panel(recipe=3)).read_state()
    assert state.state is BlenderState.READY
    assert state.recipe_state == 3


async def test_cached_device_info_skips_identification() -> None:
    info = await attach(c_panel()).identify()
    blender = c_panel()
    await attach(blender, device_info=info).read_state()
    assert (0x02, 0x081A) not in blender.reads()


async def test_read_ble_power_uses_panel_slave() -> None:
    assert await attach(c_panel()).read_ble_power() == (9, 0)
