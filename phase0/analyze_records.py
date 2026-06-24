#!/usr/bin/env python3
"""Offline record-level analyzer for H18 (dose accumulators) and H19 (RawData
sampling window) over ANY RadiaCode log (soak / h16 / matrix), using the robust
reparser in rc_reparse.py (NOT cdump's decoder, which desyncs on (0,4)).

H18 — which accumulator to use for accumulated dose, and (0,4) flag semantics:
  * extracts every RareData.dose (R -> nSv) and DoseCounter.dose_counter (uR -> nSv)
  * prints first/last/delta for each, and the DoseCounter/RareData delta RATIO
  * (0,4) flags histogram + per-flag monotonicity (0x9000 valid vs 0x1000 other)

H19 — is RawData.count_rate counts/window or cps, and what is the window:
  * value granularity (gcd of the near-integer values) — explains the "even ints"
  * native sampling window from in-buffer consecutive ts_offset diffs (10 ms step)
  * mean(RawData.count_rate) / mean(RealTimeData.count_rate) ratio (=1 -> cps;
    =N -> counts per N-second window). NB inflated if movement happened (see labels).
  * per-label breakdown if the log carries window labels (h16/matrix).

Usage:
  uv run python analyze_records.py soak_logs/<log>.jsonl.gz [--out report.md]
"""

import argparse
import gzip
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from rc_reparse import reparse_records, float_gcd


def load_events(path: Path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _stats(vals):
    if not vals:
        return 'n=0'
    if len(vals) == 1:
        return f'n=1 val={vals[0]:.4g}'
    return (f'n={len(vals)} min={min(vals):.4g} med={statistics.median(vals):.4g} '
            f'max={max(vals):.4g} mean={statistics.mean(vals):.4g}')


def analyze(path: Path) -> str:
    raw_cr = []                       # all RawData.count_rate
    raw_dr_nsv = []                   # all RawData.dose_rate -> nSv/h
    rt_cr = []                        # all RealTimeData.count_rate
    raw_by_label = defaultdict(list)  # label -> [count_rate]
    rt_by_label = defaultdict(list)
    raw_window_diffs = []             # in-buffer consecutive RawData ts_ms diffs (positive)
    dosec = []                        # (mono, dose_counter_uR, flags)
    rare = []                         # (mono, dose_R, duration_s, flags)
    dosec_flags = Counter()
    reparse_stop = Counter()
    frames = 0
    leftover_nonzero = 0
    # how many records cdump would have dropped: records after a (0,4) in a buffer
    recoverable_after_04 = 0

    for e in load_events(path):
        if e.get('ev') != 'databuf':
            continue
        raw_hex = e.get('raw')
        if not raw_hex:
            continue
        label = e.get('label')
        recs, meta = reparse_records(bytes.fromhex(raw_hex))
        frames += 1
        if meta['stop']:
            reparse_stop[meta['stop']] += 1
        if meta['leftover'] != 0:
            leftover_nonzero += 1

        seen_04 = False
        buf_raw_ts = []
        for r in recs:
            name = r['name']
            if name == 'DoseCounter':
                seen_04 = True
            elif seen_04 and name in ('RawData', 'RealTimeData', 'DoseRateDB', 'RareData'):
                recoverable_after_04 += 1

            if name == 'RawData':
                raw_cr.append(r['count_rate'])
                raw_dr_nsv.append(r.get('dose_rate_nsv_h'))
                buf_raw_ts.append(r['ts_ms'])
                if label:
                    raw_by_label[label].append(r['count_rate'])
            elif name == 'RealTimeData':
                rt_cr.append(r['count_rate'])
                if label:
                    rt_by_label[label].append(r['count_rate'])
            elif name == 'DoseCounter':
                dosec.append((e.get('mono'), r['dose_counter'], r['flags']))
                dosec_flags[r['flags_hex']] += 1
            elif name == 'RareData':
                rare.append((e.get('mono'), r['dose'], r['duration'], r['flags']))

        buf_raw_ts.sort()
        for a, b in zip(buf_raw_ts, buf_raw_ts[1:]):
            if 0 < b - a <= 60000:   # ignore reorder/wrap and cross-gaps
                raw_window_diffs.append(b - a)

    lines = []
    w = lines.append
    w('# RadiaCode record-level analysis (H18 dose accumulators, H19 RawData window)\n')
    w(f'- Log: `{path}`')
    w(f'- databuf frames re-parsed: {frames}; stop reasons: {dict(reparse_stop)}; '
      f'frames with leftover!=0: {leftover_nonzero}')
    w(f'- records recoverable AFTER a (0,4) in the same buffer (cdump would DROP these): '
      f'**{recoverable_after_04}**\n')

    # ---------------- H18 ----------------
    def rate_nsv_h(delta_nsv, span_s):
        return delta_nsv / (span_s / 3600.0) if span_s and span_s > 0 else float('nan')

    w('## H18 — dose accumulators (which one to use) + (0,4) flag semantics\n')
    w(f'- DoseCounter (0/4) records: {len(dosec)}; RareData (0/3) records: {len(rare)}')
    w(f'- (0,4) flags histogram: {dict(dosec_flags)}  '
      f'(doc said only 0x9000=valid / 0x1000=other — more variants here)')

    # DoseCounter per-flag: different flags may be DIFFERENT counters -> never mix.
    dc_main_rate = None
    if dosec:
        w('- DoseCounter uR by flag (uR->nSv = x10; rate over its own first..last span):')
        for fl in sorted({f for (_m, _v, f) in dosec}):
            grp = [(m, v) for (m, v, f) in dosec if f == fl]
            vals = [v for _m, v in grp]
            span = (grp[-1][0] - grp[0][0]) if grp[-1][0] and grp[0][0] else 0
            mono = all(b >= a for a, b in zip(vals, vals[1:]))
            dnsv = (vals[-1] - vals[0]) * 10.0
            r = rate_nsv_h(dnsv, span)
            w(f'    0x{fl:04x}: n={len(vals)} {vals[0]}..{vals[-1]} uR (delta={vals[-1]-vals[0]}uR='
              f'{dnsv:.0f}nSv) span={span:.0f}s monotonic={mono} rate={r:.1f} nSv/h')
            if fl == 0x9000:
                dc_main_rate = r
        # overall (mixed) — kept only to show why mixing is WRONG
        dc_vals = [v for (_m, v, _f) in dosec]
        mono_all = all(b >= a for a, b in zip(dc_vals, dc_vals[1:]))
        w(f'    (mixed-flag overall monotonic={mono_all} <- if False, flags are different counters; '
          f'use 0x9000 only)')

    rd_rate = None
    if rare:
        rd = [(m, v) for (m, v, _dur, _f) in rare]
        vals = [v for _m, v in rd]
        span = (rd[-1][0] - rd[0][0]) if rd[-1][0] and rd[0][0] else 0
        mono = all(b >= a for a, b in zip(vals, vals[1:]))
        dnsv = (vals[-1] - vals[0]) * 1e7
        rd_rate = rate_nsv_h(dnsv, span)
        w(f'- RareData.dose R (R->nSv = x1e7): {vals[0]:.6g}..{vals[-1]:.6g} '
          f'(delta={(vals[-1]-vals[0]):.3g}R={dnsv:.0f}nSv) span={span:.0f}s '
          f'monotonic={mono} rate={rd_rate:.1f} nSv/h')
        durs = [dur for (_m, _v, dur, _f) in rare if dur is not None]
        if durs:
            w(f'- RareData.duration (device lifetime, s): {durs[0]}..{durs[-1]} '
              f'(delta={durs[-1]-durs[0]}s vs wall span={span:.0f}s)')

    if dc_main_rate is not None and rd_rate not in (None, 0):
        w(f'- **rate ratio DoseCounter(0x9000)/RareData = {dc_main_rate / rd_rate:.3f}** '
          f'(=1 -> same physical accumulator; !=1 -> different definition)')
    w('- For background sanity: a true accumulated-dose rate should ~ the live dose_rate '
      '(~0.1 uSv/h = ~100 nSv/h at background). Whichever accumulator matches that is dose.')
    w('- NB: absolute calibration needs the device-display accumulated-dose delta over '
      'the same interval (read off the screen) — see dose-compare hardware run.\n')

    # ---------------- H19 ----------------
    w('## H19 — RawData.count_rate: counts/window or cps?\n')
    w(f'- RawData.count_rate: {_stats(raw_cr)}')
    gran = float_gcd(raw_cr) if raw_cr else None
    if raw_cr:
        all_int = all(abs(v - round(v)) < 1e-6 for v in raw_cr)
        w(f'  - all near-integer: {all_int}; granularity (gcd of values): {gran}'
          + ('' if gran is None else f'  -> values are multiples of {gran}'))
        sample = sorted(set(round(v, 3) for v in raw_cr))[:24]
        w(f'  - distinct value sample: {sample}')
    w(f'- RealTimeData.count_rate: {_stats(rt_cr)}')
    if raw_cr and rt_cr:
        ratio = statistics.mean(raw_cr) / statistics.mean(rt_cr)
        w(f'- **mean(RawData)/mean(RealTimeData) = {ratio:.3f}** '
          f'(~1 -> RawData is cps; ~N -> counts per N-second window). '
          f'CAVEAT: inflated if movement occurred (check per-label below).')
    if raw_window_diffs:
        w(f'- native in-buffer ts step between consecutive RawData: {_stats(raw_window_diffs)} ms '
          f'(median ~ sampling window)')
    else:
        w('- native in-buffer ts step: no adjacent RawData pairs found')

    if raw_by_label:
        w('\n- per-label RawData vs RealTimeData (h16/matrix logs):')
        for lbl in raw_by_label:
            rr = raw_by_label[lbl]
            tt = rt_by_label.get(lbl, [])
            w(f'  - [{lbl}] RawData {_stats(rr)} | RealTimeData {_stats(tt)}')
    w('')

    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='H18/H19 record-level analyzer (robust reparse)')
    ap.add_argument('logfile')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    path = Path(args.logfile)
    report = analyze(path)
    print(report)
    out = Path(args.out) if args.out else path.with_name(
        'records_' + path.name.replace('.jsonl.gz', '').replace('.jsonl', '') + '.md')
    out.write_text(report, encoding='utf-8')
    print(f'\n[analyze_records] report -> {out}')


if __name__ == '__main__':
    main()
