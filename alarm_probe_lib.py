"""Shared helpers for live RadiaCode alarm-limit validation (BLE/USB).

Used by ``read_alarm_limits.py`` and ``validate_alarm_limits.py``. Reads alarm
registers as raw uint32 (bypassing the radiacode lib's high-byte VSFR decode
trap for DS_UNITS / CR_UNITS / SOUND_CTRL), maps to Atom iOS internal nSv
units, and supports fixture comparison plus snapshot save/restore.
"""

from __future__ import annotations

import json
import struct
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from radiacode import RadiaCode
from radiacode.radiacode import ProtocolError
from radiacode.types import COMMAND, VSFR

# RC-101-005265, validated on macOS BLE (see timesync.py / DEVICES.local.md)
DEFAULT_BLE_UUID = '62B635D0-CFAA-1B4C-204F-D1837DEF3F68'

# Fixed device convention: 1 µR = 10 nSv (100 Sv/R).
UR_TO_NSV = 10.0

# Ground-truth fixture (RadiaCode-101, docs/radiacode-ble-protocol.md §11.5).
RC101_FIXTURE: dict[str, int] = {
    'DR_LEV1_uR_h': 35,
    'DR_LEV2_uR_h': 60,
    'DS_LEV1_uR': 25000,
    'DS_LEV2_uR': 30000,
    'CR_LEV1_cp10s': 300,
    'CR_LEV2_cp10s': 600,
    'DS_UNITS': 1,
    'CR_UNITS': 0,
}

# Same batch order as atomapp-ios AMRadiacodeDevice -readAlarmLimitsWithCompletion:
ALARM_REG_NAMES: list[str] = [
    'DR_LEV1_uR_h',
    'DR_LEV2_uR_h',
    'DS_LEV1_uR',
    'DS_LEV2_uR',
    'CR_LEV1_cp10s',
    'CR_LEV2_cp10s',
    'DS_UNITS',
    'CR_UNITS',
]

ALARM_REGS: list[tuple[str, VSFR]] = [
    (name, VSFR[name]) for name in ALARM_REG_NAMES
]

CTRL_REG_NAMES: list[str] = [
    'SOUND_CTRL',
    'VIBRO_CTRL',
    'ALARM_MODE',
    'USE_nSv_h',
]

CTRL_REGS: list[tuple[str, VSFR]] = [
    (name, VSFR[name]) for name in CTRL_REG_NAMES
]

CTRL_BITS: list[tuple[int, str]] = [
    (0, 'BUTTONS'),
    (1, 'CLICKS'),
    (2, 'DOSE_RATE_ALARM_1'),
    (3, 'DOSE_RATE_ALARM_2'),
    (4, 'DOSE_RATE_OUT_OF_SCALE'),
    (5, 'DOSE_ALARM_1'),
    (6, 'DOSE_ALARM_2'),
    (7, 'DOSE_OUT_OF_SCALE'),
    (8, 'CONN/POWER signal A (empirical)'),
    (9, 'CONN/POWER signal B (empirical)'),
]

LOW_BYTE_BOOL_REGS = frozenset({'DS_UNITS', 'CR_UNITS'})


def alarm_vsfr_by_name(name: str) -> VSFR:
    return VSFR[name]


def decode_low_byte_register(name: str, raw_u32: int) -> int:
    """Decode a register value from a batch-read uint32 word."""
    if name in LOW_BYTE_BOOL_REGS:
        return raw_u32 & 0x1
    return raw_u32


def decode_sound_ctrl(raw_u32: int) -> int:
    return raw_u32 & 0xFFFF


def decode_vibro_ctrl(raw_u32: int) -> int:
    return raw_u32 & 0xFF


def decode_alarm_mode(raw_u32: int) -> int:
    return raw_u32 & 0xFF


def decode_bits(mask: int) -> str:
    on = [name for bit, name in CTRL_BITS if mask & (1 << bit)]
    extra = mask & ~sum(1 << bit for bit, _ in CTRL_BITS)
    if extra:
        on.append(f'<unmapped 0x{extra:X}>')
    return ', '.join(on) if on else '(none)'


def bit_mask(n: int) -> int:
    return (1 << n) - 1 if n else 0


def batch_read_raw_u32(rc: RadiaCode, vsfr_ids: list[int]) -> tuple[int, list[int]]:
    """Return ``(valid_flags, raw_u32_values)`` from ``RD_VIRT_SFR_BATCH``."""
    nvsfr = len(vsfr_ids)
    msg = b''.join([struct.pack('<I', nvsfr), *[struct.pack('<I', vid) for vid in vsfr_ids]])
    buf = rc.execute(COMMAND.RD_VIRT_SFR_BATCH, msg)
    valid_flags = buf.unpack('<I')[0]
    raw: list[int] = []
    for _ in range(nvsfr):
        if buf.size() >= 4:
            raw.append(buf.unpack('<I')[0])
        else:
            break
    if buf.size() != 0:
        raise ProtocolError(f'batch_read_raw_u32: trailing bytes size={buf.size()}')
    return valid_flags, raw


def batch_write_raw_u32(rc: RadiaCode, pairs: list[tuple[int, int]]) -> int:
    """Write VSFRs via ``WR_VIRT_SFR_BATCH``. Returns ``valid_flags`` from response."""
    n = len(pairs)
    if not n:
        raise ValueError('batch_write_raw_u32: empty pairs')
    pack_items = [n] + [int(vid) for vid, _ in pairs] + [int(val) for _, val in pairs]
    pack_format = f'<I{n}I{n}I'
    resp = rc.execute(COMMAND.WR_VIRT_SFR_BATCH, struct.pack(pack_format, *pack_items))
    return resp.unpack('<I')[0]


@dataclass
class AlarmSnapshot:
    """Captured alarm + optional control registers for restore."""

    serial: str
    captured_at: str
    transport: str
    alarm_valid_flags: int
    alarm_raw: dict[str, int] = field(default_factory=dict)
    alarm_decoded: dict[str, int] = field(default_factory=dict)
    ctrl_raw: dict[str, int] = field(default_factory=dict)
    ctrl_decoded: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AlarmSnapshot:
        return cls(**data)


@dataclass
class AtomIosAlarmMapping:
    """Atom internal units (nSv/h, nSv) — matches AMRadiacodeControllerSettings."""

    threshold_dose_rate_nsv_h_l1: float
    threshold_dose_rate_nsv_h_l2: float
    threshold_dose_nsv_l1: float
    threshold_dose_nsv_l2: float
    count_rate_cps_l1: float
    count_rate_cps_l2: float
    ds_units_sv: bool
    cr_units_cpm: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_alarm_snapshot(
    rc: RadiaCode,
    *,
    transport: str = 'unknown',
    include_ctrl: bool = True,
) -> AlarmSnapshot:
    """Read the 8-register alarm batch (+ optional ctrl regs) with low-byte decode."""
    ids = [int(reg) for _, reg in ALARM_REGS]
    valid_flags, raw_words = batch_read_raw_u32(rc, ids)
    alarm_raw: dict[str, int] = {}
    alarm_decoded: dict[str, int] = {}
    for (name, _), raw in zip(ALARM_REGS, raw_words, strict=True):
        alarm_raw[name] = raw
        alarm_decoded[name] = decode_low_byte_register(name, raw)

    ctrl_raw: dict[str, int] = {}
    ctrl_decoded: dict[str, int] = {}
    if include_ctrl:
        ctrl_ids = [int(reg) for _, reg in CTRL_REGS]
        _, ctrl_words = batch_read_raw_u32(rc, ctrl_ids)
        for (name, _), raw in zip(CTRL_REGS, ctrl_words, strict=True):
            ctrl_raw[name] = raw
            if name == 'SOUND_CTRL':
                ctrl_decoded[name] = decode_sound_ctrl(raw)
            elif name == 'VIBRO_CTRL':
                ctrl_decoded[name] = decode_vibro_ctrl(raw)
            elif name == 'ALARM_MODE':
                ctrl_decoded[name] = decode_alarm_mode(raw)
            else:
                ctrl_decoded[name] = raw & 0xFF

    try:
        serial = rc.serial_number()
    except Exception:
        serial = '<unknown>'

    return AlarmSnapshot(
        serial=serial,
        captured_at=datetime.now(timezone.utc).isoformat(),
        transport=transport,
        alarm_valid_flags=valid_flags,
        alarm_raw=alarm_raw,
        alarm_decoded=alarm_decoded,
        ctrl_raw=ctrl_raw,
        ctrl_decoded=ctrl_decoded,
    )


def decode_for_atom_ios(decoded: dict[str, int]) -> AtomIosAlarmMapping:
    """Map physical register integers to Atom iOS nSv / cps (§11.5, §9.3)."""
    cr_mult = 60 if decoded['CR_UNITS'] else 1
    return AtomIosAlarmMapping(
        threshold_dose_rate_nsv_h_l1=decoded['DR_LEV1_uR_h'] * UR_TO_NSV,
        threshold_dose_rate_nsv_h_l2=decoded['DR_LEV2_uR_h'] * UR_TO_NSV,
        threshold_dose_nsv_l1=decoded['DS_LEV1_uR'] * UR_TO_NSV,
        threshold_dose_nsv_l2=decoded['DS_LEV2_uR'] * UR_TO_NSV,
        count_rate_cps_l1=decoded['CR_LEV1_cp10s'] / 10 * cr_mult,
        count_rate_cps_l2=decoded['CR_LEV2_cp10s'] / 10 * cr_mult,
        ds_units_sv=bool(decoded['DS_UNITS']),
        cr_units_cpm=bool(decoded['CR_UNITS']),
    )


def float_misread_demo(raw_u32: int) -> float:
    """Reproduce the atomapp-ios (37) float-misread bug for a threshold register."""
    return struct.unpack('<f', struct.pack('<I', raw_u32))[0]


def compare_fixture(
    decoded: dict[str, int],
    fixture: dict[str, int] | None = None,
) -> list[tuple[str, int, int | None, bool]]:
    """Return per-register ``(name, expected, actual, ok)`` rows."""
    fixture = fixture or RC101_FIXTURE
    rows: list[tuple[str, int, int | None, bool]] = []
    for name, expected in fixture.items():
        actual = decoded.get(name)
        ok = actual == expected
        rows.append((name, expected, actual, ok))
    return rows


def fixture_all_ok(rows: list[tuple[str, int, int | None, bool]]) -> bool:
    return all(ok for _, _, _, ok in rows)


def library_thresholds_match_snapshot(
    snapshot: AlarmSnapshot,
    library_limits: Any,
) -> dict[str, bool]:
    """Check radiacode ``get_alarm_limits()`` numerics against our raw decode."""
    dec = snapshot.alarm_decoded
    dose_mult = 100 if dec['DS_UNITS'] else 1
    cr_mult = 60 if dec['CR_UNITS'] else 1
    expected_dose_unit = 'Sv' if dec['DS_UNITS'] else 'R'
    expected_count_unit = 'cpm' if dec['CR_UNITS'] else 'cps'
    checks = {
        'l1_dose_rate_raw': abs(library_limits.l1_dose_rate - dec['DR_LEV1_uR_h'] / dose_mult) < 0.01,
        'l2_dose_rate_raw': abs(library_limits.l2_dose_rate - dec['DR_LEV2_uR_h'] / dose_mult) < 0.01,
        'l1_dose_scaled': abs(library_limits.l1_dose - dec['DS_LEV1_uR'] / 1e6 / dose_mult) < 1e-9,
        'l2_dose_scaled': abs(library_limits.l2_dose - dec['DS_LEV2_uR'] / 1e6 / dose_mult) < 1e-9,
        'l1_count_rate': abs(library_limits.l1_count_rate - dec['CR_LEV1_cp10s'] / 10 * cr_mult) < 0.01,
        'l2_count_rate': abs(library_limits.l2_count_rate - dec['CR_LEV2_cp10s'] / 10 * cr_mult) < 0.01,
        'dose_unit_label': library_limits.dose_unit == expected_dose_unit,
        'count_unit_label': library_limits.count_unit == expected_count_unit,
    }
    return checks


def save_snapshot(path: str | Path, snapshot: AlarmSnapshot) -> None:
    p = Path(path)
    p.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + '\n', encoding='utf-8')


def load_snapshot(path: str | Path) -> AlarmSnapshot:
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return AlarmSnapshot.from_dict(data)


def restore_snapshot(rc: RadiaCode, snapshot: AlarmSnapshot, *, restore_ctrl: bool = False) -> bool:
    """Write back raw alarm registers (and optionally ctrl) from a snapshot."""
    alarm_pairs = [(int(VSFR[name]), snapshot.alarm_raw[name]) for name in ALARM_REG_NAMES]
    expected = bit_mask(len(alarm_pairs))
    valid = batch_write_raw_u32(rc, alarm_pairs)
    ok = valid == expected
    if restore_ctrl and snapshot.ctrl_raw:
        ctrl_pairs = [(int(VSFR[name]), snapshot.ctrl_raw[name]) for name in CTRL_REG_NAMES if name in snapshot.ctrl_raw]
        if ctrl_pairs:
            ctrl_valid = batch_write_raw_u32(rc, ctrl_pairs)
            ok = ok and ctrl_valid == bit_mask(len(ctrl_pairs))
    return ok


def connect_radiacode(
    *,
    usb: bool = False,
    bluetooth_address: str | None = None,
    bluetooth_name: str | None = None,
    serial_number: str | None = None,
) -> tuple[RadiaCode, str]:
    """Open RadiaCode; return ``(device, transport_label)``."""
    if usb:
        if serial_number:
            rc = RadiaCode(serial_number=serial_number, ignore_firmware_compatibility_check=True)
        else:
            rc = RadiaCode(ignore_firmware_compatibility_check=True)
        return rc, 'usb'

    if bluetooth_name:
        rc = RadiaCode(bluetooth_name=bluetooth_name, ignore_firmware_compatibility_check=True)
        return rc, 'ble'

    addr = bluetooth_address or DEFAULT_BLE_UUID
    rc = RadiaCode(bluetooth_address=addr, ignore_firmware_compatibility_check=True)
    return rc, 'ble'
