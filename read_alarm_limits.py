#!/usr/bin/env python3
"""Read RadiaCode alarm limits + sound/vibro control; verify Atom iOS decode.

Connects over BLE (default, macOS) or USB, prints raw uint32 registers, the
low-byte decode used by atomapp-ios, ``radiacode.get_alarm_limits()`` comparison,
and optional fixture diff against the RC-101 ground truth (§11.5).

Usage (RC-101 default UUID):
  uv run python read_alarm_limits.py

  uv run python read_alarm_limits.py --usb
  uv run python read_alarm_limits.py --compare-fixture
  uv run python read_alarm_limits.py --json
"""

from __future__ import annotations

import argparse
import json
import sys

from radiacode.transports.bluetooth import DeviceNotFound

from alarm_probe_lib import (
    ALARM_REGS,
    DEFAULT_BLE_UUID,
    RC101_FIXTURE,
    compare_fixture,
    connect_radiacode,
    decode_bits,
    decode_for_atom_ios,
    fixture_all_ok,
    float_misread_demo,
    library_thresholds_match_snapshot,
    read_alarm_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Read RadiaCode alarm limits and verify decode.')
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        '--usb',
        action='store_true',
        help='Connect over USB instead of BLE',
    )
    parser.add_argument(
        '--bluetooth-address',
        default=None,
        help=f'CoreBluetooth UUID (default: RC-101 {DEFAULT_BLE_UUID})',
    )
    parser.add_argument(
        '--bluetooth-name',
        default=None,
        help='BLE name prefix scan instead of explicit address',
    )
    parser.add_argument(
        '--serial-number',
        default=None,
        help='USB serial when multiple devices are connected',
    )
    parser.add_argument(
        '--compare-fixture',
        action='store_true',
        help='Exit 1 unless all registers match the RC-101 fixture',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='Emit machine-readable JSON on stdout',
    )
    return parser


def print_human(snapshot, atom_map, library_limits, lib_checks, fixture_rows) -> None:
    print(f'Connected: serial={snapshot.serial} transport={snapshot.transport}')
    print(f'valid_flags=0x{snapshot.alarm_valid_flags:08X} expected=0x{bit_mask(len(ALARM_REGS)):08X}')
    print()

    print('== Alarm-limit registers (raw uint32, low bytes = value) ==')
    for name, _ in ALARM_REGS:
        raw = snapshot.alarm_raw[name]
        dec = snapshot.alarm_decoded[name]
        print(f'  {name:<14} = {dec}    (raw=0x{raw:08X})')
    print()

    if library_limits is not None:
        print('== radiacode.get_alarm_limits() (library decode) ==')
        print(
            f'  dose_rate : L1={library_limits.l1_dose_rate} L2={library_limits.l2_dose_rate} '
            f'({library_limits.dose_unit}/h)'
        )
        print(
            f'  dose      : L1={library_limits.l1_dose} L2={library_limits.l2_dose} '
            f'({library_limits.dose_unit})'
        )
        print(
            f'  count_rate: L1={library_limits.l1_count_rate} L2={library_limits.l2_count_rate} '
            f'({library_limits.count_unit})'
        )
        print('  NOTE: requires radiacode >= 0.4.4 (low-byte DS_UNITS/CR_UNITS decode).')
        if lib_checks:
            print('  library vs snapshot:', ' '.join(f'{k}={"OK" if v else "FAIL"}' for k, v in lib_checks.items()))
        print()

    print('== Atom internal-unit conversion (AMRadiacodeControllerSettings) ==')
    print(
        f'  thresholdDoseRateAtIndex:0 = {atom_map.threshold_dose_rate_nsv_h_l1:.0f} nSv/h '
        f'(= {atom_map.threshold_dose_rate_nsv_h_l1 / 1000:.3f} µSv/h)'
    )
    print(
        f'  thresholdDoseRateAtIndex:1 = {atom_map.threshold_dose_rate_nsv_h_l2:.0f} nSv/h '
        f'(= {atom_map.threshold_dose_rate_nsv_h_l2 / 1000:.3f} µSv/h)'
    )
    print(
        f'  thresholdDoseAtIndex:0     = {atom_map.threshold_dose_nsv_l1:.0f} nSv '
        f'(= {atom_map.threshold_dose_nsv_l1 / 1e6:.4f} mSv)'
    )
    print(
        f'  thresholdDoseAtIndex:1     = {atom_map.threshold_dose_nsv_l2:.0f} nSv '
        f'(= {atom_map.threshold_dose_nsv_l2 / 1e6:.4f} mSv)'
    )
    print()

    print('== Float-misread demo (the atomapp-ios (37) bug) ==')
    for name in ('DR_LEV1_uR_h', 'DR_LEV2_uR_h'):
        raw = snapshot.alarm_decoded[name]
        as_float = float_misread_demo(raw)
        print(f'  {name}: int={raw} -> misread-as-float={as_float:.3e}')
    print()

    print('== Sound / vibro / alarm-mode registers (raw uint32) ==')
    for name in snapshot.ctrl_decoded:
        raw = snapshot.ctrl_raw[name]
        dec = snapshot.ctrl_decoded[name]
        if name == 'SOUND_CTRL':
            print(f'  {name:<11} = 0x{dec:04X}  -> {decode_bits(dec)}')
        elif name == 'VIBRO_CTRL':
            print(f'  {name:<11} = 0x{dec:02X}  -> {decode_bits(dec)}')
        elif name == 'ALARM_MODE':
            mode = 'Once' if dec == 0 else 'Continuously'
            print(f'  {name:<11} = {dec}  (0x{raw:08X}; observed {mode})')
        else:
            print(f'  {name:<11} = {dec}  (0x{raw:08X})')
    print()

    if fixture_rows:
        print('== Verify raw registers against the RC-101 fixture ==')
        for name, expected, actual, ok in fixture_rows:
            status = 'OK  ' if ok else 'FAIL'
            print(f'  [{status}] {name:<14} expected={expected} actual={actual}')
        print()
        if fixture_all_ok(fixture_rows):
            print('RESULT: all alarm registers match the fixture.')
        else:
            print('RESULT: MISMATCH — device settings differ from RC-101 fixture.')


def bit_mask(n: int) -> int:
    return (1 << n) - 1 if n else 0


def main() -> int:
    args = build_parser().parse_args()

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

    exit_code = 0
    try:
        snapshot = read_alarm_snapshot(rc, transport=transport, include_ctrl=True)
        atom_map = decode_for_atom_ios(snapshot.alarm_decoded)

        library_limits = None
        lib_checks: dict[str, bool] = {}
        try:
            library_limits = rc.get_alarm_limits()
            lib_checks = library_thresholds_match_snapshot(snapshot, library_limits)
        except Exception as exc:
            if not args.json:
                print(f'get_alarm_limits() failed: {exc}')

        fixture_rows = compare_fixture(snapshot.alarm_decoded, RC101_FIXTURE) if args.compare_fixture else []

        if args.json:
            payload = {
                'snapshot': snapshot.to_dict(),
                'atom_ios': atom_map.to_dict(),
                'library': None if library_limits is None else {
                    'l1_count_rate': library_limits.l1_count_rate,
                    'l2_count_rate': library_limits.l2_count_rate,
                    'count_unit': library_limits.count_unit,
                    'l1_dose_rate': library_limits.l1_dose_rate,
                    'l2_dose_rate': library_limits.l2_dose_rate,
                    'l1_dose': library_limits.l1_dose,
                    'l2_dose': library_limits.l2_dose,
                    'dose_unit': library_limits.dose_unit,
                },
                'library_checks': lib_checks,
                'fixture': {
                    'enabled': args.compare_fixture,
                    'rows': [
                        {'name': n, 'expected': e, 'actual': a, 'ok': ok}
                        for n, e, a, ok in fixture_rows
                    ],
                    'all_ok': fixture_all_ok(fixture_rows) if fixture_rows else None,
                },
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print_human(snapshot, atom_map, library_limits, lib_checks, fixture_rows)

        if args.compare_fixture and fixture_rows and not fixture_all_ok(fixture_rows):
            exit_code = 1
    finally:
        try:
            rc.close()
        except Exception:
            pass

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
