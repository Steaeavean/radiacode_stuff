#!/usr/bin/env python3
"""H20 — read-only spectrum decoder validation for RadiaCode (BLE).

VS.SPECTRUM is the device's *accumulating lifetime* spectrum (its `duration` is
days), so a single read shows only the long-run background shape — no clean
photopeak. To validate end-to-end without writing to the device (no SPEC_RESET),
we take TWO reads `--window` seconds apart while a known source is held near the
detector and look at the DIFFERENCE spectrum: the source's photopeak appears in
`counts1 - counts0`, and its channel maps to energy via E = a0 + a1*ch + a2*ch^2.

Validation performed:
  * len(counts) == 1024 (proto §10.2) on both reads;
  * SpecFormatVersion (v0 raw / v1 RLE+delta) reported;
  * INDEPENDENT local re-decode of the raw bytes (port of §10.3/§10.4) cross-checked
    against cdump's decode_RC_VS_SPECTRUM — proves the byte format on fw4.14;
  * difference spectrum: top peak channel -> energy, compared to --source line.

Read-only & safe. Run:
  cd radiacode_stuff/phase0
  uv run python spectrum_probe.py --window 180 --source "Cs-137 (662 keV)"
Hold the source close & still during the wait. Log -> soak_logs/spectrum_<ts>.jsonl.gz
"""

import argparse
import asyncio
import datetime
import struct
import time
from pathlib import Path

from bleak import BleakClient

from ble_transport import RadiaCodeBLE
from ble_soak import SoakLogger, adv_to_dict, cprint, scan_matches


def _local_decode_spectrum(raw: bytes, fmt_version: int):
    """Independent re-decode (proto §10.2/§10.3/§10.4) for cross-checking cdump.

    Returns (duration_s, a0, a1, a2, counts). Header is <Ifff (16 bytes)."""
    ts, a0, a1, a2 = struct.unpack_from('<Ifff', raw, 0)
    body = raw[16:]
    counts = []
    if fmt_version == 0:
        for i in range(0, len(body) - len(body) % 4, 4):
            counts.append(struct.unpack_from('<I', body, i)[0])
    else:  # v1: RLE + delta
        pos, n, last = 0, len(body), 0
        while n - pos >= 2:
            u16 = struct.unpack_from('<H', body, pos)[0]
            pos += 2
            cnt = (u16 >> 4) & 0x0FFF
            vlen = u16 & 0x0F
            for _ in range(cnt):
                if vlen == 0:
                    v = 0
                elif vlen == 1:
                    v = body[pos]; pos += 1
                elif vlen == 2:
                    v = last + struct.unpack_from('<b', body, pos)[0]; pos += 1
                elif vlen == 3:
                    v = last + struct.unpack_from('<h', body, pos)[0]; pos += 2
                elif vlen == 4:
                    a, b = body[pos], body[pos + 1]
                    c = struct.unpack_from('<b', body, pos + 2)[0]
                    v = last + ((c << 16) | (b << 8) | a); pos += 3
                elif vlen == 5:
                    v = last + struct.unpack_from('<i', body, pos)[0]; pos += 4
                else:
                    raise ValueError(f'unsupported vlen={vlen}')
                last = v
                counts.append(v)
    return ts, a0, a1, a2, counts


def _top_peaks(counts, a0, a1, a2, k=6):
    idx = sorted(range(len(counts)), key=lambda i: counts[i], reverse=True)[:k]
    return [(i, counts[i], round(a0 + a1 * i + a2 * i * i, 1)) for i in idx]


def parse_args():
    ap = argparse.ArgumentParser(description='H20 read-only spectrum validation (BLE)')
    ap.add_argument('--window', type=float, default=180.0, help='seconds between the two reads (default 180)')
    ap.add_argument('--source', default='(unspecified)', help='isotope/source label for the energy check')
    ap.add_argument('--scan-timeout', type=float, default=12.0)
    ap.add_argument('--device-name', default='RadiaCode/RC-')
    ap.add_argument('--logdir', default='soak_logs')
    return ap.parse_args()


async def read_and_validate(rc, logger, tag):
    raw, spec = await rc.spectrum()
    fmt = rc.spectrum_format_version
    lts, la0, la1, la2, lcounts = _local_decode_spectrum(raw, fmt)
    match = (len(lcounts) == len(spec.counts) and lcounts == list(spec.counts))
    cprint(f'[{tag}] n_ch={len(spec.counts)} dur={spec.duration.total_seconds():.0f}s '
           f'sum={int(sum(spec.counts))} a0={spec.a0:.3f} a1={spec.a1:.4f} a2={spec.a2:.3e} '
           f'| local_decode_matches_cdump={match} (local n={len(lcounts)})')
    logger.log('spectrum', tag=tag, fmt_version=fmt, n_channels=len(spec.counts),
               duration_s=spec.duration.total_seconds(), a0=spec.a0, a1=spec.a1, a2=spec.a2,
               counts_sum=int(sum(spec.counts)), local_decode_matches_cdump=match,
               raw=raw.hex(), counts=list(spec.counts))
    return spec


async def amain():
    args = parse_args()
    Path(args.logdir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    logger = SoakLogger(Path(args.logdir) / f'spectrum_{stamp}.jsonl.gz')
    cprint(f'[spectrum] log -> {logger.path}  source={args.source!r}')

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
    cprint(f'CONNECTED {await rc.serial_number()} fw={(await rc.fw_version())[1]} '
           f'specfmt={rc.spectrum_format_version}')

    try:
        spec0 = await read_and_validate(rc, logger, 't0')
        cprint(f'>>> HOLD the source ({args.source}) CLOSE & STILL for {args.window:.0f}s ...')
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.window:
            await asyncio.sleep(min(10.0, args.window))
            cprint(f'   ... {int(time.monotonic() - t0)}/{int(args.window)}s')
        spec1 = await read_and_validate(rc, logger, 't1')

        n = min(len(spec0.counts), len(spec1.counts))
        diff = [spec1.counts[i] - spec0.counts[i] for i in range(n)]
        neg = sum(1 for d in diff if d < 0)
        peaks = _top_peaks(diff, spec1.a0, spec1.a1, spec1.a2)
        logger.log('spectrum_diff', source=args.source, n=n, total_diff=int(sum(diff)),
                   negative_bins=neg, top_peaks=peaks)
        cprint(f'--- DIFFERENCE spectrum over {args.window:.0f}s (source={args.source}):')
        cprint(f'    total Δcounts={int(sum(diff))}, negative bins={neg}/{n} '
               f'(should be ~0 — accumulating)')
        cprint('    top Δ peaks (channel, Δcounts, keV):')
        for ch, dc, kev in peaks:
            cprint(f'      ch={ch} Δ={dc} -> {kev} keV')
        cprint(f'    => compare the dominant Δ-peak energy to the known line of {args.source}.')
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.close()
        cprint(f'[spectrum] done -> {logger.path}')


if __name__ == '__main__':
    asyncio.run(amain())
