#!/usr/bin/env python3
"""H18 — dose-compare: which on-device accumulator equals the displayed "Dose"?

Decides whether session accumulated dose should be mapped from RareData.dose (0/3,
R) or from DoseCounter (0/4, uR). Both are lifetime accumulators (proto §14.4),
so we compare their DELTA over a fixed exposure to the DELTA of the number the
device shows on screen — the accumulator whose delta matches the display is dose.

Method (read-only):
  1. connect + init, poll DATA_BUF continuously;
  2. you read the device-screen "Dose" and type it in (t0), then again at t1;
  3. over `--minutes` hold a source near the detector (or just background);
  4. script computes delta(RareData.dose), delta(DoseCounter by flag) in nSv and
     compares each to delta(display). Ratio ~1.0 == that's the dose accumulator.

DoseCounter (0/4) has multiple flag subtypes (0x9000/0x1000/0xb000/0xd000, §14.11);
only 0x9000 looks like the valid cumulative — we track per-flag.

Run (no sudo; Bluetooth permission for the terminal):
  cd radiacode_stuff/phase0
  uv run python dose_compare.py --minutes 15 --display-unit uSv --source "thorium mantle"
Log -> soak_logs/dosecmp_<ts>.jsonl.gz  (re-analyzable with analyze_records.py)
"""

import argparse
import asyncio
import datetime
import time
from collections import defaultdict
from pathlib import Path

from bleak import BleakClient

from ble_transport import RadiaCodeBLE
from ble_soak import SoakLogger, adv_to_dict, cprint, scan_matches
from rc_reparse import reparse_records

# display unit -> nSv per unit
UNIT_NSV = {'uSv': 1e3, 'mSv': 1e6, 'uR': 10.0, 'mR': 1e4, 'nSv': 1.0}


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def poll_until(rc, logger, seconds, on_rec, label):
    """Poll DATA_BUF for `seconds`, robustly reparse, call on_rec(record) for each."""
    t0 = time.monotonic()
    last_hb = 0.0
    while time.monotonic() - t0 < seconds:
        try:
            raw, _ = await rc.data_buf()
        except Exception as ex:
            logger.log('error', where=f'dosecmp_poll:{label}', msg=str(ex))
            cprint(f'  poll err: {ex}')
            await asyncio.sleep(1.0)
            continue
        recs, meta = reparse_records(raw)
        if recs:
            logger.log('databuf', label=label, raw=raw.hex(), n=len(recs), records=recs)
        for r in recs:
            on_rec(r)
        now = time.monotonic()
        if now - last_hb >= 10.0:
            last_hb = now
            cprint(f'   [{label}] {int(now - t0)}/{int(seconds)}s ...')
        await asyncio.sleep(1.0)


def parse_args():
    ap = argparse.ArgumentParser(description='H18 dose accumulator vs display comparison (BLE)')
    ap.add_argument('--minutes', type=float, default=15.0, help='exposure window, minutes (default 15)')
    ap.add_argument('--display-unit', choices=list(UNIT_NSV), default='uSv',
                    help='unit shown by the device "Dose" field (default uSv)')
    ap.add_argument('--source', default='(background)', help='source label for the log')
    ap.add_argument('--baseline-secs', type=float, default=30.0,
                    help='pre-window poll to capture baseline accumulators (default 30)')
    ap.add_argument('--scan-timeout', type=float, default=12.0)
    ap.add_argument('--device-name', default='RadiaCode/RC-')
    ap.add_argument('--logdir', default='soak_logs')
    return ap.parse_args()


async def amain():
    args = parse_args()
    Path(args.logdir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    logger = SoakLogger(Path(args.logdir) / f'dosecmp_{stamp}.jsonl.gz')
    cprint(f'[dosecmp] log -> {logger.path}  source={args.source!r} unit={args.display_unit}')

    prefixes = [p for p in args.device_name.split('/') if p]
    cprint(f'scanning for {"/".join(prefixes)} ...')
    matches = await scan_matches(prefixes, args.scan_timeout)
    for dev, adv in matches:
        logger.log('adv', **adv_to_dict(dev, adv))
    if not matches:
        cprint('device not found'); logger.close(); return
    dev, _ = matches[0]
    client = BleakClient(dev, timeout=30.0)
    await client.connect()
    rc = RadiaCodeBLE(client)
    await rc.start_notifications()
    await rc.init_session()
    cprint(f'CONNECTED {await rc.serial_number()} fw={(await rc.fw_version())[1]}')

    # accumulators captured over the run
    rare = []                       # (mono, dose_R)
    dosec = defaultdict(list)       # flags -> [(mono, dose_counter_uR)]

    def ingest(r):
        if r['name'] == 'RareData':
            rare.append((time.monotonic(), r['dose']))
        elif r['name'] == 'DoseCounter':
            dosec[r['flags']].append((time.monotonic(), r['dose_counter']))

    try:
        cprint(f'baseline poll {args.baseline_secs:.0f}s (establishing accumulators) ...')
        await poll_until(rc, logger, args.baseline_secs, ingest, 'baseline')
        rare0 = rare[-1][1] if rare else None
        dc0 = {fl: vals[-1][1] for fl, vals in dosec.items() if vals}
        cprint(f'baseline: RareData.dose={rare0} R | DoseCounter={ {f"0x{f:04x}": v for f, v in dc0.items()} }')

        disp0 = float((await ainput(f'>>> Введи дисплейную «Дозу» прибора СЕЙЧАС ({args.display_unit}): ')).strip())
        logger.log('display', when='t0', value=disp0, unit=args.display_unit)
        cprint(f'>>> Держи источник ({args.source}) у детектора {args.minutes:.0f} мин ...')

        await poll_until(rc, logger, args.minutes * 60.0, ingest, 'exposure')

        disp1 = float((await ainput(f'>>> Введи дисплейную «Дозу» прибора СЕЙЧАС ({args.display_unit}): ')).strip())
        logger.log('display', when='t1', value=disp1, unit=args.display_unit)

        # ---- compare deltas (nSv) ----
        f = UNIT_NSV[args.display_unit]
        d_disp = (disp1 - disp0) * f
        cprint('\n================ H18 RESULT ================')
        cprint(f'display Δ = {disp1 - disp0:.4g} {args.display_unit} = {d_disp:.1f} nSv')

        d_rare = None
        if rare and rare0 is not None:
            d_rare = (rare[-1][1] - rare0) * 1e7
            ratio = d_rare / d_disp if d_disp else float('nan')
            cprint(f'RareData.dose Δ = {d_rare:.1f} nSv  (ratio vs display = {ratio:.3f})')
        for fl, vals in sorted(dosec.items()):
            if not vals:
                continue
            v0 = dc0.get(fl, vals[0][1])
            d = (vals[-1][1] - v0) * 10.0
            ratio = d / d_disp if d_disp else float('nan')
            cprint(f'DoseCounter 0x{fl:04x} Δ = {d:.1f} nSv  (n={len(vals)}, ratio vs display = {ratio:.3f})')
        cprint('-> Аккумулятор с ratio ≈ 1.0 = дисплейная «Доза». Это и есть источник дозы для маппинга.')
        cprint('============================================')
        logger.log('dosecmp_result', display_delta_nsv=d_disp,
                   rare_delta_nsv=d_rare,
                   dosec_delta_nsv={f'0x{fl:04x}': (vals[-1][1] - dc0.get(fl, vals[0][1])) * 10.0
                                    for fl, vals in dosec.items() if vals})
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.close()
        cprint(f'[dosecmp] done -> {logger.path}')


if __name__ == '__main__':
    asyncio.run(amain())
