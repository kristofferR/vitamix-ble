"""Program encoding, upload and cancel against vectors derived from the app.

The vectors come from two independent analyses of the Perfect Blend app
(7.3.5 and 7.1.0); they are synthesized from its code, not captured.
"""

from __future__ import annotations

import pytest

from vitamix_ble.client import VitamixUnsupportedError
from vitamix_ble.models import DeviceInfo, PanelType
from vitamix_ble.program import ProgramStep, encode_program, upload_requests
from vitamix_ble.protocol import encode_write

from .fake_device import FakeBlender, attach

HALF_MINUTE = [ProgramStep(power=0.5, seconds=60, ramp_seconds=1)]
C_WORDS = [0, 60, 4500, 60, 5, 2000, 0, 1, 2000, 0, 1, 2000, 0, 1]
C_INFO = DeviceInfo(PanelType.C, "Ascent A3500", 0, {"thread": (1, 3, 0)}, None)
C_1_4_INFO = DeviceInfo(PanelType.C, "Ascent A3500", 0, {"thread": (1, 4, 0)}, None)
B_INFO = DeviceInfo(PanelType.B, "Ascent A2500", 0x031D, {}, None)
OLD_B_INFO = DeviceInfo(PanelType.OLD_B, None, 0, {}, None)


def packets(panel: PanelType, words: list[int], container_id: int) -> list[str]:
    return [
        encode_write(register, values, slave=slave).hex()
        for slave, register, values in upload_requests(panel, words, container_id)
    ]


class TestEncodeProgram:
    def test_c_and_b(self) -> None:
        assert encode_program(HALF_MINUTE, PanelType.C) == C_WORDS
        assert encode_program(HALF_MINUTE, PanelType.B) == C_WORDS

    def test_old_b_packs_bytes(self) -> None:
        assert encode_program(HALF_MINUTE, PanelType.OLD_B) == [
            0x3C28,
            0x6405,
            0x0100,
            0x0064,
            0x6401,
            0x0100,
        ]

    def test_long_step_splits_at_240_seconds(self) -> None:
        steps = [ProgramStep(power=1, seconds=241, ramp_seconds=1)]
        assert (
            encode_program(steps, PanelType.C)
            == [4, 1, 22500, 240, 5, 22500, 1, 5]
            + [
                2000,
                0,
                1,
            ]
            * 2
        )

    def test_too_many_segments_rejected(self) -> None:
        steps = [ProgramStep(power=0.5, seconds=240 * 4 + 1)]
        with pytest.raises(ValueError, match="segments"):
            encode_program(steps, PanelType.C)

    def test_sub_second_steps_are_skipped(self) -> None:
        steps = [ProgramStep(power=0.2, seconds=0.5), *HALF_MINUTE]
        assert encode_program(steps, PanelType.C)[:3] == [0, 60, 4500]

    @pytest.mark.parametrize(
        "step",
        [
            ProgramStep(power=1.1, seconds=10),
            ProgramStep(power=float("nan"), seconds=10),
            ProgramStep(power=0.5, seconds=-1),
            ProgramStep(power=0.5, seconds=10, ramp_seconds=0.01),
        ],
    )
    def test_invalid_step_rejected(self, step: ProgramStep) -> None:
        with pytest.raises(ValueError):
            encode_program([step], PanelType.C)


class TestUploadRequests:
    def test_c_sequence(self) -> None:
        assert packets(PanelType.C, C_WORDS, 1) == [
            "0104020c03d00700000100",
            "010232020700003c0094113c000500d0070000",
            "01023209070100d00700000100d00700000100",
            "01023201010100",
        ]

    def test_b_sequence_ends_with_mask_and_ready(self) -> None:
        sent = packets(PanelType.B, C_WORDS, 2)
        assert len(sent) == 16
        assert sent[0] == "01670201010000"
        assert sent[-2:] == ["01673483010200", "01670200010300"]

    def test_old_b_mask(self) -> None:
        words = encode_program(HALF_MINUTE, PanelType.OLD_B)
        assert packets(PanelType.OLD_B, words, 4)[-2:] == ["01673405010800", "01673404010100"]


def b_blender(*, motor: int = 0, container: int = 1) -> FakeBlender:
    return FakeBlender(
        {(0x67, 0x3480): 1, (0x67, 0x3481): container, (0x67, 0x0100): motor},
        slaves={0x67},
    )


def c_blender(*, motor: int = 0, container: int = 1) -> FakeBlender:
    return FakeBlender(
        {(0x03, 0x0F00): 1, (0x03, 0x3640): container, (0x04, 0x0100): motor},
        slaves={0x02, 0x03, 0x04},
    )


def writes(blender: FakeBlender) -> list[str]:
    return [r.hex() for r in blender.requests if r[0] == 0x01]


class TestUploadProgram:
    async def test_b_upload_uses_detected_container(self) -> None:
        blender = b_blender(container=3)
        await attach(blender, device_info=B_INFO).upload_program(HALF_MINUTE)
        assert writes(blender)[-2] == "01673483010400"

    async def test_food_processor_rejected(self) -> None:
        blender = b_blender(container=5)
        with pytest.raises(ValueError, match="does not support"):
            await attach(blender, device_info=B_INFO).upload_program(HALF_MINUTE)
        assert writes(blender) == []

    async def test_container_time_limit(self) -> None:
        blender = b_blender(container=2)  # personal cup: 75 s
        steps = [ProgramStep(power=0.5, seconds=80)]
        with pytest.raises(ValueError, match="75 s"):
            await attach(blender, device_info=B_INFO).upload_program(steps)

    async def test_refused_while_running(self) -> None:
        with pytest.raises(VitamixUnsupportedError, match="motor"):
            await attach(b_blender(motor=1), device_info=B_INFO).upload_program(HALF_MINUTE)

    async def test_old_b_needs_container_id(self) -> None:
        blender = FakeBlender({}, slaves={0x67})
        vmx = attach(blender, device_info=OLD_B_INFO)
        with pytest.raises(VitamixUnsupportedError, match="container"):
            await vmx.upload_program(HALF_MINUTE)
        await vmx.upload_program(HALF_MINUTE, container_id=4)
        assert writes(blender)[-2:] == ["01673405010800", "01673404010100"]


class TestCancel:
    async def test_c_cancel(self) -> None:
        blender = c_blender()
        await attach(blender, device_info=C_INFO).cancel_program()
        assert writes(blender) == ["01023201010200"]

    async def test_b_cancel(self) -> None:
        blender = b_blender()
        await attach(blender, device_info=B_INFO).cancel_program()
        assert writes(blender) == ["01670200010000"]

    async def test_old_b_cancel(self) -> None:
        blender = FakeBlender({}, slaves={0x67})
        await attach(blender, device_info=OLD_B_INFO).cancel_program()
        assert writes(blender) == ["01673404010000"]

    async def test_not_while_running(self) -> None:
        blender = b_blender(motor=1)
        with pytest.raises(VitamixUnsupportedError, match="motor"):
            await attach(blender, device_info=B_INFO).cancel_program()
        assert writes(blender) == []

    @pytest.mark.parametrize(
        "info",
        [C_1_4_INFO, DeviceInfo(PanelType.C, "Ascent A3500", 0, {}, None)],
    )
    async def test_c_firmware_guard(self, info: DeviceInfo) -> None:
        blender = c_blender()
        with pytest.raises(VitamixUnsupportedError, match="cancel"):
            await attach(blender, device_info=info).cancel_program()
        assert writes(blender) == []
