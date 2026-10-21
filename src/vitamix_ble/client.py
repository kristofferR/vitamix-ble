"""Async :class:`VitamixClient` — a thin wrapper around :mod:`bleak`.

Replies carry no transaction id, so exchanges are correlated by
sequencing them: one outstanding request per connection, and any
notification that arrives while nothing is pending is dropped. After a
timeout the client reconnects before its next request, because the late
reply could otherwise be taken as that request's answer. The official app
also polls one request at a time; the device sends no unsolicited
notifications in normal operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    B_MODELS,
    C_MODEL_OTHER,
    C_MODELS,
    C_RECIPE_STATE_MARKER,
    CUSTOM_PROGRAM_MAX_STEPS,
    DFU_MAGIC,
    MAX_SPEED,
    MAX_STEP_SECONDS,
    MAX_WRITE_REGISTERS,
    MIN_SPEED,
    NOTIFY_CHARACTERISTIC_UUID,
    RECIPE_C_PRESS_PULSE,
    RECIPE_C_READY,
    RECIPE_READY,
    REG_B_CONTAINER,
    REG_B_MODEL,
    REG_BLE_POWER,
    REG_C_CONTAINER_ID,
    REG_C_CONTAINER_PRESENT,
    REG_C_MODEL,
    REG_C_RECIPE_STATE,
    REG_C_THREAD_FIRMWARE,
    REG_CUSTOM_PROGRAM_BASE,
    REG_FIRMWARE,
    REG_MOTOR,
    REG_PROGRAM_FLAG,
    REG_RECIPE,
    REG_SLEEP,
    SLAVE_B,
    SLAVE_C_NORDIC,
    SLAVE_C_NXP,
    SLAVE_C_SAMD,
    WRITE_CHARACTERISTIC_UUID,
)
from .containers import lookup_container
from .models import BlenderState, DeviceInfo, PanelType, VitamixState
from .protocol import (
    PacketStatus,
    ProtocolError,
    ReplyStatusError,
    decode_read_response,
    decode_write_response,
    describe_status,
    encode_read,
    encode_write,
)

_LOGGER = logging.getLogger(__name__)


# The official app waits 10 s for a reply once the panel is identified.
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_SCAN_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_CONNECT_ATTEMPTS = 3
# Panel detection: the app waits 2 s for the C-panel probe (a timeout means
# B) and re-sends it after an error reply.
DETECTION_TIMEOUT = 2.0
DETECTION_ATTEMPTS = 3
# The app only accepts a C container id after this many identical reads.
C_CONTAINER_SAMPLES = 3

_SLEEPING = VitamixState(BlenderState.SLEEPING, True, None, None, None, False, None)
_NO_CONTAINER = VitamixState(BlenderState.NO_CONTAINER, False, False, None, None, False, None)


class VitamixError(RuntimeError):
    """Base class for vitamix-ble runtime errors."""


class VitamixTimeoutError(VitamixError):
    """Raised when a request does not produce a response in time."""


class VitamixRejectedError(VitamixError):
    """Raised when the device answers a request with a failure status."""

    def __init__(self, code: int, operation: str) -> None:
        super().__init__(f"{operation} rejected, status {code}: {describe_status(code)}")
        self.code = code


class VitamixReadRejectedError(VitamixRejectedError):
    """Raised when the device answers a read with only an error status."""

    def __init__(self, code: int) -> None:
        super().__init__(code, "read")


class VitamixUnsupportedError(VitamixError):
    """Raised for panels or operations the official app does not support."""


class VitamixWriteRejectedError(VitamixRejectedError):
    """Raised when the device returns a nonzero status for a write."""

    def __init__(self, status: PacketStatus) -> None:
        super().__init__(status.code, "write")
        self.status = status


class VitamixClient:
    """High-level async client for a single Vitamix blender."""

    def __init__(
        self,
        ble_device: BLEDevice | str,
        *,
        device_info: DeviceInfo | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        connect_attempts: int = DEFAULT_CONNECT_ATTEMPTS,
    ) -> None:
        """Create a client.

        Args:
            ble_device: A discovered :class:`BLEDevice` (preferred; connects
                with retries via ``bleak-retry-connector``) or an address.
            device_info: A previous :meth:`identify` result for this device,
                to skip identification on short-lived connections.
            connect_timeout: Timeout for address-only connections. With a
                :class:`BLEDevice`, ``bleak-retry-connector`` sets the
                per-attempt timeout.
            connect_attempts: The connector's ``max_attempts``. It caps
                timeouts and hard failures; the connector retries transient
                errors (device not found, no free slot) up to its own limit.
        """
        self._ble_device = ble_device
        self._info = device_info
        self._request_timeout = request_timeout
        self._connect_timeout = connect_timeout
        self._connect_attempts = connect_attempts
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future[bytes] | None = None
        self._stale = False  # a request timed out; its reply may still arrive
        self._generation = 0  # bumped per connection; older callbacks are ignored

    # -- construction ------------------------------------------------------

    @classmethod
    async def from_address(
        cls,
        address: str,
        *,
        scan_timeout: float = DEFAULT_SCAN_TIMEOUT,
        **kwargs: object,
    ) -> Self:
        """Resolve ``address`` into a BLEDevice via a fresh scan, then return a client.

        On Linux/BlueZ the underlying scan can take a while because Vitamix
        blenders advertise sparingly while idle. Callers that already have a
        cached :class:`BLEDevice` should pass it to ``__init__`` directly.
        """
        device = await BleakScanner.find_device_by_address(address, timeout=scan_timeout)
        if device is None:
            # Fall back to direct connect by address: BlueZ will use its
            # own cache if the device is known.
            return cls(address, **kwargs)  # type: ignore[arg-type]
        return cls(device, **kwargs)  # type: ignore[arg-type]

    # -- async context-manager protocol ------------------------------------

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Open the BLE connection and subscribe to notifications."""
        if self._client is not None and self._client.is_connected:
            return
        if isinstance(self._ble_device, BLEDevice):
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._ble_device.name or self._ble_device.address,
                max_attempts=self._connect_attempts,
            )
        else:
            self._client = BleakClient(self._ble_device, timeout=self._connect_timeout)
            await self._client.connect()
        self._stale = False
        try:
            self._generation += 1
            generation = self._generation

            def on_notify(characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
                if generation != self._generation:
                    _LOGGER.debug("dropping notification from a previous connection")
                    return
                self._on_notify(characteristic, data)

            await self._client.start_notify(NOTIFY_CHARACTERISTIC_UUID, on_notify)
            # Give the device a moment to settle before the first request.
            await asyncio.sleep(0.1)
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Stop notifications and close the BLE connection."""
        if self._client is None:
            return
        try:
            with contextlib.suppress(Exception):
                # bleak best-effort; some backends raise on stop_notify after
                # the device has already disconnected.
                await self._client.stop_notify(NOTIFY_CHARACTERISTIC_UUID)
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # -- low-level register IO ---------------------------------------------

    async def read_registers(
        self,
        register: int,
        count: int = 1,
        *,
        slave: int = SLAVE_B,
        timeout: float | None = None,
    ) -> list[int]:
        """Read ``count`` consecutive 16-bit registers starting at ``register``."""
        request = encode_read(register, count, slave=slave)
        response = await self._exchange(request, timeout)
        try:
            values, status = decode_read_response(response, count)
        except ReplyStatusError as exc:
            raise VitamixReadRejectedError(exc.code) from exc
        except ProtocolError as exc:
            raise VitamixError(f"could not parse read response {response.hex()}: {exc}") from exc
        if status:
            _LOGGER.debug(
                "read 0x%04x returned status %d with complete data %s",
                register,
                status,
                response.hex(),
            )
        return values

    async def read_register(self, register: int, *, slave: int = SLAVE_B) -> int:
        """Convenience: read a single register and return its value."""
        return (await self.read_registers(register, 1, slave=slave))[0]

    async def write_registers(
        self,
        register: int,
        values: list[int] | tuple[int, ...],
        *,
        slave: int = SLAVE_B,
    ) -> PacketStatus:
        """Write one or more 16-bit values starting at ``register``.

        Raises:
            VitamixWriteRejectedError: if the device returned a nonzero status.
        """
        request = encode_write(register, values, slave=slave)
        response = await self._exchange(request)
        status = decode_write_response(response)
        if not status.ok:
            raise VitamixWriteRejectedError(status)
        return status

    async def write_register(
        self, register: int, value: int, *, slave: int = SLAVE_B
    ) -> PacketStatus:
        """Convenience wrapper around :meth:`write_registers`."""
        return await self.write_registers(register, [value], slave=slave)

    # -- high-level helpers -------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo | None:
        """The last :meth:`identify` result, if any."""
        return self._info

    async def identify(self) -> DeviceInfo:
        """Detect the panel type, model and firmware like the official app.

        Read-only: the app also writes the BLE power register here, which
        this method skips. A C-panel answers the first probe; a timeout
        means B; an error reply is retried.
        """
        for _ in range(DETECTION_ATTEMPTS):
            try:
                (model_code,) = await self.read_registers(
                    REG_C_MODEL, slave=SLAVE_C_SAMD, timeout=DETECTION_TIMEOUT
                )
            except VitamixTimeoutError:
                info = await self._identify_b()
                break
            except VitamixRejectedError as err:
                _LOGGER.debug("panel probe failed, retrying: %s", err)
                continue
            info = await self._identify_c(model_code)
            break
        else:
            raise VitamixError("panel detection failed: every probe returned an error")
        self._info = info
        return info

    async def _identify_c(self, model_code: int) -> DeviceInfo:
        firmware: dict[str, tuple[int, ...]] = {}
        for name, slave, register, count in (
            ("samd", SLAVE_C_SAMD, REG_FIRMWARE, 2),
            ("thread", SLAVE_C_SAMD, REG_C_THREAD_FIRMWARE, 3),
            ("nxp", SLAVE_C_NXP, REG_FIRMWARE, 2),
            ("nordic", SLAVE_C_NORDIC, REG_FIRMWARE, 2),
        ):
            words = await self._optional_read(register, count, slave)
            if words is not None:
                firmware[name] = words
        return DeviceInfo(
            panel=PanelType.C,
            model=C_MODELS.get(model_code, C_MODEL_OTHER),
            model_code=model_code,
            firmware=firmware,
            ble_power=await self._optional_read(REG_BLE_POWER, 2, SLAVE_C_NORDIC),
        )

    async def _identify_b(self) -> DeviceInfo:
        ble_power = await self._optional_read(REG_BLE_POWER, 2, SLAVE_B)
        model_code = await self.read_register(REG_B_MODEL)
        if model_code == 0:
            return DeviceInfo(PanelType.OLD_B, None, 0, {}, ble_power)
        return DeviceInfo(PanelType.B, B_MODELS.get(model_code), model_code, {}, ble_power)

    async def _optional_read(self, register: int, count: int, slave: int) -> tuple[int, ...] | None:
        try:
            return tuple(await self.read_registers(register, count, slave=slave))
        except VitamixError as err:  # rejected, timed out or malformed
            _LOGGER.debug("optional read %#04x/%#06x failed: %s", slave, register, err)
            return None

    async def _require_info(self) -> DeviceInfo:
        info = self._info or await self.identify()
        if not info.supported:
            raise VitamixUnsupportedError(f"unrecognised B-panel model 0x{info.model_code:04x}")
        return info

    async def read_ble_power(self) -> tuple[int, ...]:
        """Read the BLE power register (raw words; units unknown)."""
        info = await self._require_info()
        slave = SLAVE_C_NORDIC if info.panel is PanelType.C else SLAVE_B
        return tuple(await self.read_registers(REG_BLE_POWER, 2, slave=slave))

    async def read_state(self) -> VitamixState:
        """Poll the registers the official app polls for this panel."""
        info = await self._require_info()
        if info.panel is PanelType.C:
            return await self._read_state_c(info)
        if info.panel is PanelType.B:
            return await self._read_state_b()
        return VitamixState.unknown()

    async def _read_state_c(self, info: DeviceInfo) -> VitamixState:
        if await self.read_register(REG_SLEEP, slave=SLAVE_C_NORDIC) == 1:
            return _SLEEPING
        presence = await self.read_register(REG_C_CONTAINER_PRESENT, slave=SLAVE_C_NXP)
        if presence == 0:
            return _NO_CONTAINER
        if presence != 1:
            _LOGGER.debug("invalid NFC state %d", presence)
            return VitamixState(BlenderState.UNKNOWN, False, None, None, None, None, None)
        samples = {
            await self.read_register(REG_C_CONTAINER_ID, slave=SLAVE_C_NXP)
            for _ in range(C_CONTAINER_SAMPLES)
        }
        container_id = samples.pop() if len(samples) == 1 else None
        motor = (await self.read_registers(REG_MOTOR, 5, slave=SLAVE_C_NORDIC))[0] == 1
        recipe: int | None = None
        if info.thread_1_4:
            words = await self.read_registers(REG_C_RECIPE_STATE, 4, slave=SLAVE_C_SAMD)
            recipe = words[1] if words[0] == C_RECIPE_STATE_MARKER else None
        if motor:
            summary = BlenderState.RUNNING
        elif recipe == RECIPE_C_PRESS_PULSE:
            summary = BlenderState.PRESS_PULSE
        elif recipe == RECIPE_C_READY:
            summary = BlenderState.READY
        else:
            summary = BlenderState.IDLE
        return _awake(summary, container_id, motor, recipe)

    async def _read_state_b(self) -> VitamixState:
        if await self.read_register(REG_SLEEP) == 1:
            return _SLEEPING
        presence, container_id = await self.read_registers(REG_B_CONTAINER, 2)
        if presence != 1:
            return _NO_CONTAINER
        motor = await self.read_register(REG_MOTOR) == 1
        recipe = await self.read_register(REG_RECIPE)
        if motor:
            summary = BlenderState.RUNNING
        elif recipe == RECIPE_READY:
            summary = BlenderState.READY
        else:
            summary = BlenderState.IDLE
        return _awake(summary, container_id, motor, recipe)

    async def cancel_program(self) -> PacketStatus:
        """Send the official "cancel current program" command.

        This is the same packet the Vitamix Perfect Blend app emits in
        ``cancelCurrentProgram`` for C-panel devices. Idempotent if the
        device is already idle.
        """
        return await self.write_register(REG_RECIPE, 0)

    async def load_program(self, slot: int) -> PacketStatus:
        """Arm a saved program slot.

        The Ascent / Venturist series ships with several factory program
        slots (Smoothie, Frozen Dessert, Spreads…). Writing the slot
        number to register :data:`REG_RECIPE` is the same primitive the
        Perfect Blend app's ``setBlenderProgram`` uses as the very last
        step after staging the program-step buffer.

        For built-in saved programs the staging is unnecessary because
        the firmware already has them, so this single write is enough to
        select the slot.

        Whether the motor *also* spins immediately depends on whether
        the user has the dial in an "armed" position (see
        :data:`REG_PANEL_ARMED`). On an idle / un-armed device the slot
        is just primed; the user still has to physically engage Start.

        Args:
            slot: 1-indexed saved-program slot. ``0`` is reserved for
                "no program" — use :meth:`cancel_program` for that to
                make intent explicit.

        Raises:
            ValueError: if ``slot`` is not strictly positive.
            VitamixWriteRejectedError: if the firmware rejects the
                write (e.g. invalid slot number for this model).
        """
        if slot <= 0:
            raise ValueError(f"slot must be >= 1; use cancel_program() for slot 0 (got {slot})")
        return await self.write_register(REG_RECIPE, slot)

    # -- custom-program / motor control (0.3.0+) ----------------------------
    #
    # The Ascent C-panel exposes a 12-u16 "scratch" program buffer at
    # registers 0x0201..0x020C and fires the staged buffer when a bitmask
    # is written to register 0x3483 (REG_PROGRAM_FLAG). Each step is a
    # (speed, time) pair, so we can build anything from a single
    # constant-speed run to a 6-step recipe by uploading the buffer and
    # then writing ``1 << (step_count - 1)`` to REG_PROGRAM_FLAG.
    #
    # Speed values use the same 0..10 dial scale the firmware exposes on
    # its physical interface; time values are in seconds.

    async def upload_custom_program(
        self,
        steps: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    ) -> None:
        """Stage a custom program of up to 6 (speed, time) steps.

        This writes the step buffer to registers 0x0201.. but does NOT
        start the program — call :meth:`commit_custom_program` (or one of
        the higher-level helpers like :meth:`set_motor_speed`) afterwards
        to fire it.

        Args:
            steps: list of ``(speed, seconds)`` tuples. ``speed`` must be
                in the range 0..10 (Vitamix variable-speed dial). 0 stops
                the motor; 10 is full speed. ``seconds`` is a u16 timer
                (max ``0xFFFF`` ≈ 18h, used as "run forever" sentinel).

        Raises:
            ValueError: if ``steps`` is empty, contains too many entries,
                or has speed/time values outside the supported range.
        """
        self._validate_steps(steps)
        # Flatten into the wire layout: [speed_0, time_0, speed_1, ...].
        payload: list[int] = []
        for speed, seconds in steps:
            payload.append(speed)
            payload.append(seconds)
        # The full block is 12 u16s on the wire — pad unused slots with
        # zeros so the firmware sees a deterministic state and old data
        # from a previous program can't leak into the next run.
        while len(payload) < CUSTOM_PROGRAM_MAX_STEPS * 2:
            payload.append(0)
        # Chunk to what fits in one default-MTU write.
        for offset in range(0, len(payload), MAX_WRITE_REGISTERS):
            slice_ = payload[offset : offset + MAX_WRITE_REGISTERS]
            await self.write_registers(REG_CUSTOM_PROGRAM_BASE + offset, slice_)

    async def commit_custom_program(self, step_count: int) -> PacketStatus:
        """Fire the staged custom program.

        Writes ``1 << (step_count - 1)`` to :data:`REG_PROGRAM_FLAG`. The
        firmware uses the bitmask both as a "go" trigger and as the
        step-count signal, mirroring the official app's panel-2 path.

        Args:
            step_count: must match the number of populated steps from the
                preceding :meth:`upload_custom_program` call.

        Raises:
            ValueError: if ``step_count`` is not in 1..6.
        """
        if not 1 <= step_count <= CUSTOM_PROGRAM_MAX_STEPS:
            raise ValueError(f"step_count must be 1..{CUSTOM_PROGRAM_MAX_STEPS} (got {step_count})")
        bitmask = 1 << (step_count - 1)
        return await self.write_register(REG_PROGRAM_FLAG, bitmask)

    async def run_custom_program(
        self,
        steps: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    ) -> PacketStatus:
        """Upload + commit a custom program in one shot.

        Equivalent to::

            await self.upload_custom_program(steps)
            await self.commit_custom_program(len(steps))
        """
        self._validate_steps(steps)
        await self.upload_custom_program(steps)
        return await self.commit_custom_program(len(steps))

    async def set_motor_speed(
        self,
        speed: int,
        *,
        duration_seconds: int = MAX_STEP_SECONDS,
    ) -> PacketStatus:
        """Run the motor at ``speed`` for up to ``duration_seconds``.

        Internally this stages and fires a 1-step custom program. Calling
        :meth:`set_motor_speed` again while the motor is already running
        re-stages the buffer with the new speed and refires it — that's
        the protocol-level mechanism we use for live speed changes
        (0.3.0) and for melody playback (0.5.0).

        Args:
            speed: 0..10. ``0`` stops the motor (use :meth:`stop_motor`
                for that to make intent explicit; this method delegates).
            duration_seconds: u16 seconds. Defaults to the firmware
                "run forever" sentinel ``0xFFFF`` so you can call
                :meth:`stop_motor` at your leisure.

        Raises:
            ValueError: if ``speed`` is out of range.
        """
        if speed == 0:
            return await self.stop_motor()
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be {MIN_SPEED}..{MAX_SPEED} (got {speed})")
        if not 0 < duration_seconds <= MAX_STEP_SECONDS:
            raise ValueError(
                f"duration_seconds must be 1..{MAX_STEP_SECONDS} (got {duration_seconds})"
            )
        return await self.run_custom_program([(speed, duration_seconds)])

    async def start_motor(
        self,
        speed: int = 5,
        *,
        duration_seconds: int = MAX_STEP_SECONDS,
    ) -> PacketStatus:
        """Start the motor at ``speed`` (alias for :meth:`set_motor_speed`).

        Defaults to a mid-range Variable-5 so a bare ``start_motor()``
        call is gentle enough not to launch ingredients out of an open
        container.
        """
        return await self.set_motor_speed(speed, duration_seconds=duration_seconds)

    async def stop_motor(self) -> PacketStatus:
        """Stop the motor.

        Identical wire packet to :meth:`cancel_program` (writes 0 to
        REG_RECIPE) — the firmware halts the motor as a side-effect of
        unloading the program. Kept as a separate name so high-level
        callers can express intent unambiguously.
        """
        return await self.cancel_program()

    async def play_melody(
        self,
        notes: list[tuple[int, float]] | tuple[tuple[int, float], ...],
        *,
        gap_seconds: float = 0.0,
        stop_when_done: bool = True,
    ) -> None:
        """Play a sequence of (speed, duration_seconds) "notes".

        Each note re-stages a 1-step custom program at the requested
        speed, runs it for the requested duration, then advances. This
        is the primitive the Vitamix-blender-music community demos use
        to coax recognisable tunes out of the variable-speed motor.

        Args:
            notes: list of ``(speed, seconds)`` pairs. ``speed`` is
                0..10 (0 = silence/stop). ``seconds`` is the dwell at
                that speed; floats are accepted for sub-second timing.
            gap_seconds: optional silence inserted between consecutive
                notes (motor stops then restarts). Defaults to 0 so
                back-to-back notes blend smoothly.
            stop_when_done: stop the motor at the end of the sequence.
                Set to False if you want to chain another play_melody
                call without spinning down.

        Raises:
            ValueError: if ``notes`` is empty, or any speed/duration is
                out of range.
        """
        if not notes:
            raise ValueError("notes must not be empty")
        for speed, seconds in notes:
            if not MIN_SPEED <= speed <= MAX_SPEED:
                raise ValueError(f"speed must be {MIN_SPEED}..{MAX_SPEED} (got {speed})")
            if seconds <= 0:
                raise ValueError(f"duration must be > 0 seconds (got {seconds})")
        try:
            for speed, seconds in notes:
                if speed == 0:
                    await self.stop_motor()
                else:
                    await self.set_motor_speed(speed)
                await asyncio.sleep(seconds)
                if gap_seconds > 0:
                    await self.stop_motor()
                    await asyncio.sleep(gap_seconds)
        finally:
            if stop_when_done:
                with contextlib.suppress(Exception):
                    await self.stop_motor()

    @staticmethod
    def _validate_steps(
        steps: list[tuple[int, int]] | tuple[tuple[int, int], ...],
    ) -> None:
        if not steps:
            raise ValueError("steps must not be empty")
        if len(steps) > CUSTOM_PROGRAM_MAX_STEPS:
            raise ValueError(
                f"at most {CUSTOM_PROGRAM_MAX_STEPS} steps supported (got {len(steps)})"
            )
        for index, (speed, seconds) in enumerate(steps):
            if not MIN_SPEED <= speed <= MAX_SPEED:
                raise ValueError(
                    f"step {index}: speed must be {MIN_SPEED}..{MAX_SPEED} (got {speed})"
                )
            if not 0 <= seconds <= MAX_STEP_SECONDS:
                raise ValueError(
                    f"step {index}: time must be 0..{MAX_STEP_SECONDS} (got {seconds})"
                )

    # -- internals ----------------------------------------------------------

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise VitamixError("client is not connected")
        return self._client

    async def _exchange(self, request: bytes, timeout: float | None = None) -> bytes:
        """Send one request and return its reply; one request at a time."""
        if request == DFU_MAGIC:
            raise VitamixError("refusing to send the firmware-update magic")
        async with self._lock:
            if self._stale:
                await self.disconnect()
                await self.connect()
            client = self._require_client()
            self._pending = asyncio.get_running_loop().create_future()
            try:
                await client.write_gatt_char(WRITE_CHARACTERISTIC_UUID, request, response=True)
                timeout = self._request_timeout if timeout is None else timeout
                return await asyncio.wait_for(self._pending, timeout=timeout)
            except TimeoutError as exc:
                self._stale = True
                raise VitamixTimeoutError(
                    f"no response to {request.hex()} within {timeout}s"
                ) from exc
            except BaseException:
                # Failed or cancelled after sending: the reply may still arrive.
                self._stale = True
                raise
            finally:
                self._pending = None

    def _on_notify(self, _characteristic: BleakGATTCharacteristic, data: bytearray) -> None:
        pending = self._pending
        if pending is None or pending.done():
            _LOGGER.debug("dropping unsolicited or late notification %s", bytes(data).hex())
            return
        pending.set_result(bytes(data))


def _awake(
    summary: BlenderState, container_id: int | None, motor: bool, recipe: int | None
) -> VitamixState:
    return VitamixState(
        state=summary,
        sleeping=False,
        container_present=True,
        container_id=container_id,
        container=None if container_id is None else lookup_container(container_id),
        motor_running=motor,
        recipe_state=recipe,
    )


@asynccontextmanager
async def open_vitamix(
    address: str,
    **kwargs: object,
) -> AsyncIterator[VitamixClient]:
    """Convenience: ``async with open_vitamix(addr) as vmx:``."""
    client = await VitamixClient.from_address(address, **kwargs)  # type: ignore[arg-type]
    async with client as connected:
        yield connected
