#!/usr/bin/env python3
"""Probe which VSFR id the device accepts for SYS_FW_VER_BT (BT module firmware).

Compares candidate register addresses after the 0.4.1 audit changed
``VSFR.SYS_FW_VER_BT`` from ``0xFFFF010`` to ``0xFFFF0010``.

Usage (RC-101 default UUID from local validation):
  uv run python probe_sys_fw_ver_bt.py

  uv run python probe_sys_fw_ver_bt.py --bluetooth-address <uuid>
"""

from __future__ import annotations

import argparse
import struct
import sys

from radiacode import RadiaCode
from radiacode.radiacode import ProtocolError
from radiacode.transports.bluetooth import DeviceNotFound
from radiacode.types import COMMAND, VSFR

# RC-101-005265, validated on macOS BLE (see timesync.py / DEVICES.local.md)
DEFAULT_BLE_UUID = '62B635D0-CFAA-1B4C-204F-D1837DEF3F68'

# Candidates: audit fix, upstream typo, next slot in 0xFFFF00xx sequence
CANDIDATES: list[tuple[str, int]] = [
    ('audit_fix_0xFFFF0010', 0xFFFF0010),
    ('upstream_typo_0xFFFF010', 0xFFFF010),
    ('sequence_next_0xFFFF000E', 0xFFFF000E),
    ('current_enum', int(VSFR.SYS_FW_VER_BT)),
]

CONTROL_VSFR = VSFR.SYS_MCU_TEMP  # known-good neighbour (0xFFFF000D)


def batch_read_raw(rc: RadiaCode, vsfr_ids: list[int]) -> tuple[int, list[int], bytes]:
    """Return (valid_flags, raw_values, trailing_bytes) from RD_VIRT_SFR_BATCH."""
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
    return valid_flags, raw, buf.data()


def bit_mask(n: int) -> int:
    return (1 << n) - 1 if n else 0


def describe_flags(valid_flags: int, n: int) -> str:
    expected = bit_mask(n)
    ok = valid_flags == expected
    per_bit = ' '.join(f'{i}:{"ok" if valid_flags & (1 << i) else "fail"}' for i in range(n))
    return f'valid_flags=0x{valid_flags:08X} expected=0x{expected:08X} {"PASS" if ok else "FAIL"} ({per_bit})'


def decode_version_u32(raw: int) -> str:
    """Best-effort: many SYS_* version regs pack maj/min/patch in bytes."""
    b = struct.pack('<I', raw)
    return f'raw=0x{raw:08X} bytes={b.hex(" ")} as_u32={raw}'


def main() -> None:
    parser = argparse.ArgumentParser(description='Probe SYS_FW_VER_BT VSFR address on live hardware.')
    parser.add_argument(
        '--bluetooth-address',
        default=DEFAULT_BLE_UUID,
        help=f'CoreBluetooth UUID (default: RC-101 {DEFAULT_BLE_UUID})',
    )
    parser.add_argument(
        '--bluetooth-name',
        default=None,
        help='BLE name prefix scan instead of explicit address',
    )
    args = parser.parse_args()

    print('Connecting…')
    try:
        if args.bluetooth_name:
            rc = RadiaCode(bluetooth_name=args.bluetooth_name, ignore_firmware_compatibility_check=True)
        else:
            rc = RadiaCode(
                bluetooth_address=args.bluetooth_address,
                ignore_firmware_compatibility_check=True,
            )
    except DeviceNotFound as exc:
        print(f'ERROR: device not found: {exc}', file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f'ERROR: connect failed: {exc}', file=sys.stderr)
        sys.exit(1)

    try:
        (boot_major, boot_minor, _), (target_major, target_minor, _) = rc.fw_version()
        print(f'Connected. MCU fw {boot_major}.{boot_minor}, target {target_major}.{target_minor}')
        bt_major, bt_minor = rc.bt_fw_version()
        print(f'BT module fw {bt_major}.{bt_minor} (via bt_fw_version())')
        print(f'Enum VSFR.SYS_FW_VER_BT = {int(VSFR.SYS_FW_VER_BT):#x}')
        print()

        # Control: neighbour register must read cleanly
        print('--- control (SYS_MCU_TEMP) ---')
        flags, raw, tail = batch_read_raw(rc, [int(CONTROL_VSFR)])
        print(describe_flags(flags, 1))
        if raw:
            print(decode_version_u32(raw[0]))
        if tail:
            print(f'trailing: {tail.hex(" ")}')
        print()

        print('--- SYS_FW_VER_BT candidates (single register each) ---')
        winners: list[str] = []
        for label, vid in CANDIDATES:
            print(f'[{label}] id={vid:#x}')
            try:
                flags, raw, tail = batch_read_raw(rc, [vid])
            except (ProtocolError, ValueError, Exception) as exc:
                print(f'  ERROR: {exc}')
                print()
                continue
            print(f'  {describe_flags(flags, 1)}')
            if flags == 1 and raw:
                print(f'  {decode_version_u32(raw[0])}')
                winners.append(label)
            elif flags == 0:
                print('  register not accepted (bit 0 clear)')
            else:
                print(f'  partial/unexpected flags, raw={raw!r}')
            if tail:
                print(f'  trailing: {tail.hex(" ")}')
            print()

        # Batch: all candidates + control in one request (see per-bit failures)
        ids = [int(CONTROL_VSFR)] + [vid for _, vid in CANDIDATES]
        labels = ['SYS_MCU_TEMP'] + [label for label, _ in CANDIDATES]
        print('--- combined batch (control + all candidates) ---')
        flags, raw, tail = batch_read_raw(rc, ids)
        print(describe_flags(flags, len(ids)))
        for i, (lab, val) in enumerate(zip(labels, raw, strict=False)):
            bit_ok = bool(flags & (1 << i))
            print(f'  [{i}] {lab}: {"ok" if bit_ok else "fail"}  {decode_version_u32(val) if bit_ok else ""}')
        if tail:
            print(f'trailing: {tail.hex(" ")}')
        print()

        if winners:
            print(f'RESULT: device responded on: {", ".join(winners)}')
        else:
            print('RESULT: no candidate returned valid_flags bit 0 — BT FW register may be unreadable or use another id')
    finally:
        try:
            rc.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
