"""Constants for the Vitamix BLE protocol.

All values were derived by observing the live wire protocol and from
disassembly of the official Vitamix Perfect Blend Android app's native
``libcore.so`` (``VitamixBLEBlender`` class).
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# GATT
# ---------------------------------------------------------------------------

SERVICE_UUID: Final = "4a310001-cf3c-4cb9-bed6-2ea871b8d070"
WRITE_CHARACTERISTIC_UUID: Final = "4a310002-cf3c-4cb9-bed6-2ea871b8d070"
NOTIFY_CHARACTERISTIC_UUID: Final = "4a310003-cf3c-4cb9-bed6-2ea871b8d070"

# Local-name advertised by the device.
ADV_LOCAL_NAME: Final = "Vitamix_2.0"

# Default ATT MTU. A plain Write Request or notification carries at most
# ``ATT_MTU - 3`` value bytes, and the official app never negotiates more.
DEFAULT_MTU: Final = 23
MAX_PAYLOAD_BYTES: Final = DEFAULT_MTU - 3  # = 20


# ---------------------------------------------------------------------------
# Wire protocol — function codes (object offsets 0x200/0x201 in libcore.so).
# ---------------------------------------------------------------------------

FN_READ: Final = 0x02
FN_WRITE: Final = 0x01

# Single-byte ACK returned after a successful write.
WRITE_ACK_OK: Final = 0x00

# Nonzero reply statuses the official app names (``readCharacteristic``).
# Other nonzero values are possible but undocumented.
DEVICE_ERRORS: Final[dict[int, str]] = {
    11: "read: wrong slave",
    12: "read: wrong function",
    13: "read: wrong register",
    14: "read: wrong length",
    15: "write: wrong slave",
    16: "write: wrong function",
    17: "write: wrong register",
    18: "write: CRC wrong (internal base/panel link)",
    19: "write: wrong length",
    20: "character timeout between base and panel",
}

# The app's ``enterDFU`` writes exactly these bytes to the normal write
# characteristic, which presumably puts the panel into firmware-update
# mode. Never send them.
DFU_MAGIC: Final = bytes.fromhex("deadbeef")


# ---------------------------------------------------------------------------
# Slave addresses. The official app identifies the panel at runtime (see
# ``VitamixClient.identify``): C-panels (Ascent A3300/A3500) spread their
# registers over three boards, B-panels (Ascent A2300/A2500, Venturist)
# answer on one address.
# ---------------------------------------------------------------------------

SLAVE_C_SAMD: Final = 0x02  # C main board: model, Thread firmware, programs
SLAVE_C_NXP: Final = 0x03  # C NFC board: container detection
SLAVE_C_NORDIC: Final = 0x04  # C radio board: sleep, motor, BLE power
SLAVE_B: Final = 0x67  # every B-panel and old-B register


# ---------------------------------------------------------------------------
# Identification (read during ``identify``).
# ---------------------------------------------------------------------------

# Read on SLAVE_C_SAMD. Its outcome selects the panel: a reply means C, a
# timeout means B. The value is the C UI model.
REG_C_MODEL: Final = 0x081A  # u16: 0 = A3500 (UI-C1), nonzero = A3300 (UI-C2)
REG_C_THREAD_FIRMWARE: Final = 0x0818  # u16 x 3; (1, 4, ...) changes polling/cancel
REG_FIRMWARE: Final = 0x0001  # u16 x 2 on each C board (SAMD, NXP, Nordic)
REG_B_MODEL: Final = 0x3482  # u16: B model word, 0 = old B
REG_BLE_POWER: Final = 0x34FC  # u16 x 2; the app writes 9 here (units unknown)

C_MODELS: Final[dict[int, str]] = {0: "Ascent A3500"}
C_MODEL_OTHER: Final = "Ascent A3300"
B_MODELS: Final[dict[int, str]] = {
    0x075D: "Venturist 350",
    0x0501: "Venturist 330",
    0x0301: "Ascent A2300",
    0x0101: "Ascent A2300",
    0x031D: "Ascent A2500",
}


# ---------------------------------------------------------------------------
# Status polled by the official app.
# ---------------------------------------------------------------------------

REG_SLEEP: Final = 0x347F  # u16: 1 = panel asleep (C: Nordic, B: 0x67)
REG_MOTOR: Final = 0x0100  # u16 word 0: 1 = motor running (C reads 5 words)
REG_C_CONTAINER_PRESENT: Final = 0x0F00  # u16 on NXP: 0 = none, 1 = present
REG_C_CONTAINER_ID: Final = 0x3640  # u16 on NXP: raw container id
REG_C_RECIPE_STATE: Final = 0x3220  # u16 x 4 on SAMD, Thread 1.4 only
C_RECIPE_STATE_MARKER: Final = 101  # word 0 when word 1 holds the recipe state
REG_B_CONTAINER: Final = 0x3480  # u16 x 2: (1 = present, raw container id)
REG_RECIPE: Final = 0x0200  # B recipe state: 0 = idle, 3 = program ready

# Recipe states the app's UI reacts to.
RECIPE_READY: Final = 3  # B: turn the dial to 1, then press start
RECIPE_C_PRESS_PULSE: Final = 4  # C Thread 1.4
RECIPE_C_READY: Final = 5  # C Thread 1.4: press start


# ---------------------------------------------------------------------------
# Unverified registers. Earlier live probing on slave 0x67 found plausible
# motor values here, but the official app never reads them on any panel.
# ---------------------------------------------------------------------------

REG_MOTOR_RPM_LIVE: Final = 0x0101  # u16: live blade RPM?
REG_MOTOR_MAX_RPM: Final = 0x010A  # u16: max RPM? (e.g. 12000)
REG_MOTOR_RATED_RPM: Final = 0x010B  # u16: rated RPM? (e.g. 5000)
REG_MOTOR_MAX_PERCENT: Final = 0x0111  # u16: 100?
REG_MOTOR_RATED_W: Final = 0x0113  # u16: rated watts? (e.g. 1200)


# Custom-program "step" buffer (Ascent C-panel, panel-type 2).
# The Perfect Blend app's ``setBlenderProgram`` panel-2 path writes one
# u16 per consecutive register starting at 0x0201, then triggers the
# upload by writing a bitmask to :data:`REG_PROGRAM_FLAG`. Each step is
# encoded as a (speed, time) pair, so:
#
#     0x0201  step 0 speed       0x0202  step 0 time
#     0x0203  step 1 speed       0x0204  step 1 time
#     ...
#
# A maximum of 6 steps fit in the 12-u16 buffer (0x0201..0x020C).
REG_CUSTOM_PROGRAM_BASE: Final = 0x0201  # first u16 of the step buffer
CUSTOM_PROGRAM_MAX_STEPS: Final = 6  # 12 u16s ÷ 2 (speed,time) = 6 steps

# Program-load registers (used by the official app's setBlenderProgram).
# Writing to REG_RECIPE with a non-zero slot value arms a saved program;
# the device then waits for a physical "Start" press to spin the blade.
# REG_PROGRAM_FLAG is the panel-2 "fire" register: writing a bitmask
# ``1 << (step_count - 1)`` finalises and starts the previously-uploaded
# custom program in 0x0201..0x020C.
REG_PROGRAM_FLAG: Final = 0x3483  # u16: bitmask, fires the staged custom program
REG_PROGRAM_STEP_BASE_PANEL3: Final = 0x3406  # u16 x N: panel-type-3 step buffer
REG_PROGRAM_LOAD_CMD: Final = 0x3404  # u16: panel-3 "begin load"
REG_PROGRAM_READY: Final = 0x3405  # u16: panel-3 program-ready flag

# Speed / time encoding for custom-program steps. Vitamix dials run from
# 1 ("Variable 1", lowest) to 10 ("Variable 10", highest); 0 means "off".
# The firmware accepts those values verbatim in the speed slot.
MIN_SPEED: Final = 0  # 0 = motor off
MAX_SPEED: Final = 10  # full speed (Variable 10 / High)

# Time slot is u16 seconds. 0xFFFF = ~18h, used as "run forever" sentinel.
MAX_STEP_SECONDS: Final = 0xFFFF


# ---------------------------------------------------------------------------
# Limits derived from disassembly + wire observation.
# ---------------------------------------------------------------------------

# Register counts that keep requests and replies within one default-MTU
# payload: a write is 5 header bytes + 2 per word, a read reply is 2 + 2 per
# word. The official app never writes more than 7 words at once.
MAX_WRITE_REGISTERS: Final = (MAX_PAYLOAD_BYTES - 5) // 2  # = 7
MAX_READ_REGISTERS: Final = (MAX_PAYLOAD_BYTES - 2) // 2  # = 9
