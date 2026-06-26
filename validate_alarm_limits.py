#!/usr/bin/env python3
"""Round-trip validation for RadiaCode alarm-limit registers (H5–H9).

Captures a snapshot of the live device, optionally writes distinct test
thresholds (off by default — use ``--write-test``), verifies read-back, then
restores the original snapshot so the device ends unchanged.

Checks:
  H5 — DS_UNITS low-byte decode (Sv/R selector)
  H6 — CR_UNITS low-byte decode (cpm/cps selector)
  H7 — Atom nSv mapping (µR × 10)
  H8 — ``get_alarm_limits()`` numerics vs raw snapshot
  H9 — ``set_alarm_limits()`` round-trip (only with ``--write-test``)

Usage:
  uv run python validate_alarm_limits.py
  uv run python validate_alarm_limits.py --write-test
  uv run python validate_alarm_limits.py --usb --snapshot /tmp/alarms.json
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from radiacode.transports.bluetooth import DeviceNotFound

from alarm_probe_lib import (
    DEFAULT_BLE_UUID,
    RC101_FIXTURE,
    connect_radiacode,
    decode_for_atom_ios,
    library_thresholds_match_snapshot,
    load_snapshot,
    read_alarm_snapshot,
    restore_snapshot,
    save_snapshot,
)

# Safe offsets from the RC-101 fixture — still below typical alarm levels at background.
TEST_DR_L1 = RC101_FIXTURE['DR_LEV1_uR_h'] + 1
TEST_DR_L2 = RC101_FIXTURE['DR_LEV2_uR_h'] + 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Round-trip RadiaCode alarm-limit validation (H5–H9).')
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument('--usb', action='store_true', help='Connect over USB')
    parser.add_argument('--bluetooth-address', default=None, help=f'BLE UUID (default RC-101 {DEFAULT_BLE_UUID})')
    parser.add_argument('--bluetooth-name', default=None, help='BLE name prefix scan')
    parser.add_argument('--serial-number', default=None, help='USB serial filter')
    parser.add_argument(
        '--snapshot',
        default=None,
        metavar='PATH',
        help='Save/load snapshot JSON (default: temp file, deleted on success)',
    )
    parser.add_argument(
        '--write-test',
        action='store_true',
        help='Actually write test DR thresholds and verify round-trip (default: dry-run, read-only)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Alias for default behaviour (no writes); kept for explicit CLI parity',
    )
    return parser


def check_h5_h6(snapshot) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    for name in ('DS_UNITS', 'CR_UNITS'):
        raw = snapshot.alarm_raw[name]
        dec = snapshot.alarm_decoded[name]
        low = raw & 0x1
        passed = dec == low
        ok = ok and passed
        lines.append(f'  H5/H6 {name}: decoded={dec} raw&1={low} {"PASS" if passed else "FAIL"}')
    return ok, lines


def check_h7(snapshot) -> tuple[bool, list[str]]:
    atom = decode_for_atom_ios(snapshot.alarm_decoded)
    dec = snapshot.alarm_decoded
    lines = [
        f'  H7 DR L1: {dec["DR_LEV1_uR_h"]} µR/h -> {atom.threshold_dose_rate_nsv_h_l1:.0f} nSv/h',
        f'  H7 DS L1: {dec["DS_LEV1_uR"]} µR -> {atom.threshold_dose_nsv_l1:.0f} nSv',
    ]
    expected_dr = dec['DR_LEV1_uR_h'] * 10
    expected_ds = dec['DS_LEV1_uR'] * 10
    passed = (
        abs(atom.threshold_dose_rate_nsv_h_l1 - expected_dr) < 0.01
        and abs(atom.threshold_dose_nsv_l1 - expected_ds) < 0.01
    )
    lines.append(f'  H7 mapping {"PASS" if passed else "FAIL"}')
    return passed, lines


def check_h8(rc, snapshot) -> tuple[bool, list[str]]:
    try:
        al = rc.get_alarm_limits()
    except Exception as exc:
        return False, [f'  H8 get_alarm_limits() failed: {exc}']
    checks = library_thresholds_match_snapshot(snapshot, al)
    passed = all(checks.values())
    lines = [f'  H8 {k}: {"PASS" if v else "FAIL"}' for k, v in checks.items()]
    return passed, lines


def check_h9(rc, snapshot, *, do_write: bool) -> tuple[bool, list[str]]:
    if not do_write:
        return True, ['  H9 round-trip: SKIPPED (dry-run; pass --write-test to enable)']

    lines: list[str] = []
    try:
        wrote = rc.set_alarm_limits(l1_dose_rate=TEST_DR_L1, l2_dose_rate=TEST_DR_L2)
    except Exception as exc:
        return False, [f'  H9 set_alarm_limits() failed: {exc}']

    if not wrote:
        return False, ['  H9 set_alarm_limits() returned False']

    after = read_alarm_snapshot(rc, transport=snapshot.transport, include_ctrl=False)
    dr1_ok = after.alarm_decoded['DR_LEV1_uR_h'] == TEST_DR_L1
    dr2_ok = after.alarm_decoded['DR_LEV2_uR_h'] == TEST_DR_L2
    lines.append(f'  H9 read-back DR_LEV1: expected={TEST_DR_L1} actual={after.alarm_decoded["DR_LEV1_uR_h"]} '
                 f'{"PASS" if dr1_ok else "FAIL"}')
    lines.append(f'  H9 read-back DR_LEV2: expected={TEST_DR_L2} actual={after.alarm_decoded["DR_LEV2_uR_h"]} '
                 f'{"PASS" if dr2_ok else "FAIL"}')

    restored = restore_snapshot(rc, snapshot, restore_ctrl=False)
    final = read_alarm_snapshot(rc, transport=snapshot.transport, include_ctrl=False)
    restore_dr_ok = (
        final.alarm_decoded['DR_LEV1_uR_h'] == snapshot.alarm_decoded['DR_LEV1_uR_h']
        and final.alarm_decoded['DR_LEV2_uR_h'] == snapshot.alarm_decoded['DR_LEV2_uR_h']
    )
    lines.append(f'  H9 restore batch valid_flags ok: {"PASS" if restored else "FAIL"}')
    lines.append(f'  H9 post-restore DR match snapshot: {"PASS" if restore_dr_ok else "FAIL"}')
    passed = dr1_ok and dr2_ok and restored and restore_dr_ok
    return passed, lines


def main() -> int:
    args = build_parser().parse_args()
    do_write = args.write_test and not args.dry_run

    print('Connecting…')
    try:
        rc, transport = connect_radiacode(
            usb=args.usb,
            bluetooth_address=args.bluetooth_address,
            bluetooth_name=args.bluetooth_name,
            serial_number=args.serial_number,
        )
    except DeviceNotFound as exc:
        print(f'ERROR: device not found: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'ERROR: connect failed: {exc}', file=sys.stderr)
        return 1

    snapshot_path: Path | None = Path(args.snapshot) if args.snapshot else None
    temp_path: Path | None = None
    if snapshot_path is None:
        tmp = tempfile.NamedTemporaryFile(prefix='radiacode_alarms_', suffix='.json', delete=False)
        temp_path = Path(tmp.name)
        snapshot_path = temp_path
        tmp.close()

    all_ok = True
    try:
        print(f'Capturing snapshot -> {snapshot_path}')
        before = read_alarm_snapshot(rc, transport=transport, include_ctrl=True)
        save_snapshot(snapshot_path, before)
        print(f'  serial={before.serial} valid_flags=0x{before.alarm_valid_flags:08X}')
        print()

        results: list[tuple[str, bool, list[str]]] = []
        for label, fn in (
            ('H5/H6 unit low-byte decode', lambda: check_h5_h6(before)),
            ('H7 Atom nSv mapping', lambda: check_h7(before)),
            ('H8 library vs snapshot', lambda: check_h8(rc, before)),
            ('H9 write round-trip', lambda: check_h9(rc, before, do_write=do_write)),
        ):
            passed, lines = fn()
            all_ok = all_ok and passed
            print(f'== {label} ==')
            print('\n'.join(lines))
            print()
            results.append((label, passed, lines))

        if do_write:
            # check_h9 already restored; reload snapshot file for audit
            reloaded = load_snapshot(snapshot_path)
            final = read_alarm_snapshot(rc, transport=transport, include_ctrl=False)
            unchanged = final.alarm_decoded == {
                k: reloaded.alarm_decoded[k] for k in reloaded.alarm_decoded
            }
            print('== Post-test full restore ==')
            print(f'  alarm registers match saved snapshot: {"PASS" if unchanged else "FAIL"}')
            all_ok = all_ok and unchanged
        else:
            print('Dry-run: device left unchanged (no writes).')
    finally:
        try:
            rc.close()
        except Exception:
            pass
        if temp_path is not None and all_ok:
            try:
                temp_path.unlink()
            except OSError:
                pass

    if all_ok:
        print('RESULT: H5–H9 checks PASSED.')
        return 0
    print('RESULT: one or more H5–H9 checks FAILED.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
