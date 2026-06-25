#!/usr/bin/env python3
"""Sync RadiaCode device RTC to macOS (or system) local time.

The device has no GET_TIME command — only SET_TIME.  The library already
calls set_local_time(now) on every connect, but this script does it
explicitly so you can see confirmation and optionally schedule it as a
cron job or launchd task.

Typical use-case: after the official RadiaCode Android app last ran, the
device RTC is set to UTC; the device display then shows 09:00 instead of
12:00 (Moscow UTC+3).  Running this script corrects it.

Usage (from repo root):
  # Auto-scan:
  uv run python timesync.py

  # Fastest — connect by known CoreBluetooth UUID:
  uv run python timesync.py --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68

  # Estimate drift only, do not sync:
  uv run python timesync.py --dry-run
"""

import argparse
import datetime
import sys
import time

from radiacode import RadiaCode
from radiacode.transports.bluetooth import DeviceNotFound
from radiacode.types import RealTimeData


def _hms(seconds: float) -> str:
    sign = '-' if seconds < 0 else '+'
    s = abs(int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f'{sign}{h}h {m:02d}m {sec:02d}s'
    if m:
        return f'{sign}{m}m {sec:02d}s'
    return f'{sign}{sec}s'


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Sync RadiaCode device RTC to system local time.',
    )
    parser.add_argument(
        '--bluetooth-address',
        metavar='UUID',
        default=None,
        help='CoreBluetooth UUID or BLE address (avoids scan)',
    )
    parser.add_argument(
        '--bluetooth-name',
        metavar='PREFIX',
        default=None,
        help='BLE name prefix for auto-scan, e.g. "RadiaCode"',
    )
    parser.add_argument(
        '--threshold',
        type=float,
        default=5.0,
        metavar='S',
        help='Sync only when |drift| > S seconds (default: 5); use 0 to always sync',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Connect and report drift only; do not write SET_TIME to device',
    )
    args = parser.parse_args()

    if not args.bluetooth_address and not args.bluetooth_name:
        args.bluetooth_name = 'RadiaCode'

    t_system = datetime.datetime.now()
    print(f'System time:  {t_system:%Y-%m-%d %H:%M:%S}')
    print('Connecting...', flush=True)

    try:
        rc = RadiaCode(
            bluetooth_address=args.bluetooth_address,
            bluetooth_name=args.bluetooth_name,
        )
    except DeviceNotFound as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)

    serial = rc.serial_number()
    fw = rc.fw_version()[1]
    print(f'Device:       {serial}  fw {fw[0]}.{fw[1]}')

    # Wait for a fresh RealTimeData record (device emits one every ~1–2 s)
    time.sleep(3)
    records = rc.data_buf()
    rt_records = [r for r in records if isinstance(r, RealTimeData)]

    now = datetime.datetime.now()
    if rt_records:
        latest = max(rt_records, key=lambda r: r.dt)
        # DATA_BUF timestamps are computed from base_time = connect_time + 128s.
        # Drift here reflects PC clock jitter + BLE latency — not device RTC per se,
        # since the library already called set_local_time() in __init__.
        # A non-trivial drift (> threshold) suggests the PC time jumped or the
        # device was connected to software that uses UTC.
        drift = (now - latest.dt).total_seconds()
        print(f'Latest record:{latest.dt:%Y-%m-%d %H:%M:%S}  (drift {_hms(drift)})')
    else:
        drift = None
        print('Latest record: (none yet)')

    should_sync = args.threshold == 0 or drift is None or abs(drift) > args.threshold

    if args.dry_run:
        action = 'would sync' if should_sync else f'would skip (|drift| ≤ {args.threshold}s)'
        print(f'\n[dry-run] {action} to {now:%H:%M:%S}')
    elif should_sync:
        t_sync = datetime.datetime.now()
        rc.set_local_time(t_sync)
        print(f'\nSynced:       device RTC → {t_sync:%Y-%m-%d %H:%M:%S}')
    else:
        print(f'\nIn sync — skipped (|drift| ≤ {args.threshold}s).')

    try:
        rc.close()
    except Exception:
        pass


if __name__ == '__main__':
    main()
