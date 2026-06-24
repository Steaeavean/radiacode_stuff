#!/usr/bin/env python3
"""Offline analyzer for the RadiaCode BLE soak log (Phase 0 validation).

Reads a soak_<...>.jsonl(.gz) produced by ble_soak.py and produces a Markdown
report (soak_logs/report_<ts>.md) + console summary, covering the open Phase 0
questions:

  - Identity stability across (re)connects (serial / fw / fw_signature / spec fmt)
  - H1/H2/H3: advertised service UUID, advertised name, negotiated MTU
  - DATA_BUF eid/gid histogram + independent permissive re-parse of raw payloads
    to flag UNKNOWN / uncertain groups and size/structure mismatches vs §9.2
    (this is the "did we capture everything" check)
  - Cadences: RealTimeData / RawData intervals, and the real RareData/DoseRateDB
    period (closes the ">5 min" open number)
  - Charge / temperature drift over the run
  - H15: RareData dose/duration before vs after each reconnect
  - VSFR availability matrix (confirms CPS/DR_uR_h/DS_uR absent, TEMP_degC present)
  - Timestamp (H13): decreasing-dt (wrap/reorder) statistics per session
  - Errors / disconnects, transport fidelity (max frame length -> H8 evidence)

Usage:
  uv run python analyze_soak.py soak_logs/soak_<ts>.jsonl.gz [--out report.md]
"""

import argparse
import gzip
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

# Fixed-size DATA_BUF groups (bytes after the 7-byte <BBBi header), from
# docs/radiacode-ble-protocol.md §9.2 / cdump decoders/databuf.py.
GROUP_SIZE = {
    (0, 0): 15,  # RealTimeData  <ffHHHB
    (0, 1): 8,   # RawData       <ff
    (0, 2): 16,  # DoseRateDB    <IffHH
    (0, 3): 14,  # RareData      <IfHHH
    (0, 4): 6,   # DoseCounter   <IH  (uint32 uR + uint16 flags) — decoded from BLE soak (proto §14.8)
    (0, 6): 6,   # AccelData     <HHH
    (0, 7): 4,   # Event         <BBH
    (0, 8): 6,   # RawCountRate  <fH
    (0, 9): 6,   # RawDoseRate   <fH
}
GROUP_NAME = {
    (0, 0): 'RealTimeData', (0, 1): 'RawData', (0, 2): 'DoseRateDB', (0, 3): 'RareData',
    (0, 4): 'DoseCounter', (0, 5): 'SheduleData?', (0, 6): 'AccelData', (0, 7): 'Event',
    (0, 8): 'RawCountRate', (0, 9): 'RawDoseRate',
    (1, 1): 'svc/1.1', (1, 2): 'svc/1.2', (1, 3): 'svc/1.3',  # variable-size service records
}
# Layouts the cdump lib guesses but we still do NOT trust (0/5 SheduleData unconfirmed).
# (0,4) was decoded as <IH (6 bytes) from the BLE soak, so it is no longer uncertain.
UNCERTAIN = {(0, 5)}


def reparse_databuf(raw: bytes) -> dict:
    """Permissive independent walk of a DATA_BUF payload.

    Steps by known group sizes; stops (like the firmware/lib) on seq-jump,
    truncation, or an unknown/uncertain group, recording why + a raw sample so
    unmapped structures can be decoded by hand.
    """
    res = {'groups': Counter(), 'unknown': [], 'stop': None, 'leftover': 0}
    pos, n, prev_seq = 0, len(raw), None
    while n - pos >= 7:
        seq, eid, gid = raw[pos], raw[pos + 1], raw[pos + 2]
        key = (eid, gid)
        if prev_seq is not None and (prev_seq + 1) % 256 != seq:
            res['stop'] = 'seq_jump'
            break
        prev_seq = seq
        if key in UNCERTAIN:
            res['groups'][key] += 1
            res['unknown'].append({'key': list(key), 'reason': 'uncertain_layout',
                                   'after_header_hex': raw[pos + 7:pos + 7 + 40].hex()})
            res['stop'] = 'uncertain_group'
            break
        if eid == 0 and key in GROUP_SIZE:
            sz = GROUP_SIZE[key]
            if n - pos - 7 < sz:
                res['stop'] = 'trunc'
                break
            res['groups'][key] += 1
            pos += 7 + sz
        elif eid == 1 and gid in (1, 2, 3):
            if n - pos - 7 < 6:
                res['stop'] = 'trunc'
                break
            samples = int.from_bytes(raw[pos + 7:pos + 9], 'little')
            k = {1: 8, 2: 16, 3: 14}[gid]
            sz = 6 + k * samples
            if n - pos - 7 < sz:
                res['stop'] = 'trunc'
                break
            res['groups'][key] += 1
            pos += 7 + sz
        else:
            res['groups'][key] += 1
            res['unknown'].append({'key': list(key), 'reason': 'unknown_group',
                                   'after_header_hex': raw[pos + 7:pos + 7 + 40].hex()})
            res['stop'] = 'unknown_group'
            break
    res['leftover'] = n - pos
    return res


def load_events(path: Path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _fmt_stats(values):
    if not values:
        return 'n=0'
    if len(values) == 1:
        return f'n=1 val={values[0]:.1f}'
    return (f'n={len(values)} min={min(values):.1f} med={statistics.median(values):.1f} '
            f'max={max(values):.1f} mean={statistics.mean(values):.1f}')


def analyze(path: Path) -> str:
    identities = []          # (tag, dict)
    advs = []                # adv dicts
    mtus = []
    record_types = Counter()
    rare = []                # (mono, dose_R, duration_s, temp, charge)
    rt_monos = []            # RealTimeData host monos
    raw_monos = []           # RawData host monos
    dose_db_monos = []       # DoseRateDB host monos
    spectra = []             # (mono, duration_s, counts_sum)
    vsfr_union = {}          # name -> ever-ok bool, last value
    reconnects = []          # (n, begin_mono, end_mono)
    errors = []
    disconnects = 0
    frame_max_rx = 0
    frame_count = 0
    reparse_groups = Counter()
    reparse_stop = Counter()
    reparse_unknown = []     # samples
    reparse_frames = 0
    reparse_full = 0
    # ts wrap / reorder per session
    dt_decreases = 0
    dt_total = 0
    last_dt = None
    cur_recon = {}

    for e in load_events(path):
        ev = e.get('ev')
        mono = e.get('mono', 0.0)
        if ev == 'session_start':
            identities.append((e.get('tag'), {
                'serial': e.get('serial'), 'fw_target': tuple(e.get('fw_target') or []),
                'fw_signature': e.get('fw_signature'), 'spec_format_version': e.get('spec_format_version'),
            }))
            if e.get('mtu'):
                mtus.append(e['mtu'])
        elif ev == 'adv':
            advs.append(e)
        elif ev == 'disconnect':
            disconnects += 1
        elif ev == 'reconnect':
            if e.get('phase') == 'begin':
                cur_recon = {'n': e.get('n'), 'begin': mono}
            elif e.get('phase') == 'end':
                cur_recon['end'] = mono
                reconnects.append(cur_recon)
                cur_recon = {}
        elif ev == 'error':
            errors.append(e)
        elif ev == 'frame':
            frame_count += 1
            if e.get('dir') == 'rx':
                frame_max_rx = max(frame_max_rx, len(e.get('hex', '')) // 2)
        elif ev == 'vsfr_probe':
            for name, info in (e.get('matrix') or {}).items():
                prev = vsfr_union.get(name, {'ok': False, 'value': None})
                vsfr_union[name] = {
                    'ok': prev['ok'] or bool(info.get('ok')),
                    'value': info.get('value') if info.get('ok') else prev['value'],
                }
        elif ev == 'spectrum':
            spectra.append((mono, e.get('duration_s'), e.get('counts_sum')))
        elif ev == 'databuf':
            # independent re-parse of the raw payload
            raw_hex = e.get('raw')
            if raw_hex:
                rp = reparse_databuf(bytes.fromhex(raw_hex))
                reparse_frames += 1
                reparse_groups.update(rp['groups'])
                if rp['stop']:
                    reparse_stop[rp['stop']] += 1
                if rp['leftover'] == 0:
                    reparse_full += 1
                for u in rp['unknown']:
                    if len(reparse_unknown) < 20:
                        reparse_unknown.append(u)
            # decoded records
            for r in e.get('records', []):
                t = r.get('type')
                record_types[t] += 1
                if t == 'RealTimeData':
                    rt_monos.append(mono)
                elif t == 'RawData':
                    raw_monos.append(mono)
                elif t == 'DoseRateDB':
                    dose_db_monos.append(mono)
                elif t == 'RareData':
                    rare.append((mono, r.get('dose'), r.get('duration'),
                                 r.get('temperature'), r.get('charge_level')))
                # dt monotonicity (H13) across the whole stream
                dt = r.get('dt')
                if dt is not None:
                    dt_total += 1
                    if last_dt is not None and dt < last_dt:
                        dt_decreases += 1
                    last_dt = dt

    # ---- derive ----
    def diffs(monos):
        return [b - a for a, b in zip(monos, monos[1:]) if b >= a]

    rare_monos = [m for (m, *_rest) in rare]

    lines = []
    w = lines.append
    w(f'# RadiaCode BLE soak report\n')
    w(f'- Log: `{path}`')
    w(f'- Events parsed: databuf frames={reparse_frames}, frames(tx/rx)={frame_count}, '
      f'reconnects={len(reconnects)}, errors={len(errors)}, disconnects={disconnects}\n')

    w('## Identity stability')
    uniq = {(d['serial'], d['fw_target'], d['fw_signature'], d['spec_format_version']) for _, d in identities}
    w(f'- session_start snapshots: {len(identities)}; distinct identities: {len(uniq)} '
      f'({"STABLE" if len(uniq) <= 1 else "DRIFT!"})')
    for tag, d in identities[:3]:
        w(f'  - [{tag}] serial={d["serial"]} fw_target={d["fw_target"]} spec_fmt={d["spec_format_version"]}')
    w('')

    w('## H1/H2/H3 advertisement + MTU')
    adv_named = Counter(a.get('name') for a in advs)
    svc_adv = sum(1 for a in advs if a.get('service_uuid_advertised'))
    w(f'- adv captures: {len(advs)}; service `e63215e5-...` advertised in {svc_adv}/{len(advs)} '
      f'(H1: {"YES" if svc_adv else "NO -> broad scan needed"})')
    w(f'- advertised names (H2): {dict(adv_named)}')
    w(f'- MTU (H3): {_fmt_stats([float(m) for m in mtus])}')
    w('')

    w('## DATA_BUF groups (decoded record types)')
    for t, c in record_types.most_common():
        w(f'- {t}: {c}')
    w('')

    w('## Independent re-parse of raw DATA_BUF (did we capture everything?)')
    w(f'- frames re-parsed: {reparse_frames}; fully consumed (leftover=0): {reparse_full}')
    w(f'- stop reasons: {dict(reparse_stop)}')
    w('- group histogram (eid/gid):')
    for key, c in reparse_groups.most_common():
        name = GROUP_NAME.get(tuple(key), '???')
        flag = '  <-- UNKNOWN/UNMAPPED' if tuple(key) not in GROUP_NAME else (
            '  <-- UNCERTAIN LAYOUT' if tuple(key) in UNCERTAIN else '')
        w(f'  - {tuple(key)} {name}: {c}{flag}')
    if reparse_unknown:
        w('- raw samples after header for unknown/uncertain groups (decode by hand):')
        for u in reparse_unknown[:10]:
            w(f'  - {tuple(u["key"])} ({u["reason"]}): {u["after_header_hex"]}')
    w('')

    w('## Cadences (host-time intervals, s)')
    w(f'- RealTimeData: {_fmt_stats(diffs(rt_monos))}')
    w(f'- RawData: {_fmt_stats(diffs(raw_monos))}')
    w(f'- DoseRateDB: {_fmt_stats(diffs(dose_db_monos))}')
    w(f'- RareData: {_fmt_stats(diffs(rare_monos))}  <- closes the ">5 min" open number')
    w('')

    w('## Charge / temperature drift (from RareData)')
    charges = [c for (_m, _d, _dur, _t, c) in rare if c is not None]
    temps = [t for (_m, _d, _dur, t, _c) in rare if t is not None]
    w(f'- charge_level: {_fmt_stats(charges)}')
    w(f'- temperature: {_fmt_stats(temps)}')
    if 'TEMP_degC' in vsfr_union and vsfr_union['TEMP_degC']['value'] is not None:
        w(f'- VSFR.TEMP_degC last on-demand value: {vsfr_union["TEMP_degC"]["value"]}')
    w('')

    w('## H15 reconnect behaviour (RareData dose/duration before vs after)')
    if not reconnects:
        w('- no reconnects recorded')
    for rc in reconnects:
        n, b, e2 = rc.get('n'), rc.get('begin'), rc.get('end')
        before = [x for x in rare if b is not None and x[0] <= b]
        after = [x for x in rare if e2 is not None and x[0] >= e2]
        bef = before[-1] if before else None
        aft = after[0] if after else None
        if bef and aft:
            verdict = ('CONTINUES' if (aft[2] or 0) >= (bef[2] or 0) and (aft[1] or 0) >= (bef[1] or 0)
                       else 'RESET?' if (aft[2] or 0) < (bef[2] or 0) * 0.5 else 'check')
            w(f'- reconnect#{n}: before dose={bef[1]} dur={bef[2]}s | after dose={aft[1]} dur={aft[2]}s -> {verdict}')
        else:
            w(f'- reconnect#{n}: insufficient RareData around it (before={bool(bef)}, after={bool(aft)})')
    w('')

    w('## VSFR availability matrix')
    for name, info in vsfr_union.items():
        w(f'- {name}: {"OK" if info["ok"] else "absent"}' + (f' (value={info["value"]})' if info['ok'] else ''))
    w('')

    w('## Timestamps (H13)')
    w(f'- decoded records with dt: {dt_total}; dt-decreases (wrap/reorder): {dt_decreases} '
      f'({100.0 * dt_decreases / dt_total:.1f}%)' if dt_total else '- no dt records')
    w('')

    w('## Transport fidelity (H8 BLE)')
    w(f'- tx/rx frames logged: {frame_count}; max rx payload: {frame_max_rx} bytes '
      f'({"multi-fragment reassembly exercised" if frame_max_rx > 18 else "no large frames seen"})')
    w('')

    w('## Errors')
    err_kinds = Counter(e.get('where') for e in errors)
    w(f'- total: {len(errors)}; by where: {dict(err_kinds)}')
    for e in errors[:10]:
        w(f'  - [{e.get("where")}] {e.get("msg")}')
    w('')

    w('## Suggested protocol-doc deltas')
    w('- Confirm/adjust §14 against the numbers above (RareData period, MTU, adv name).')
    if any(tuple(k) in UNCERTAIN for k in reparse_groups):
        w('- Decode GRP_DoseCounter (0/4) from the raw samples above; fix §9.2 / §14.4.')
    unmapped = [tuple(k) for k in reparse_groups if tuple(k) not in GROUP_NAME]
    if unmapped:
        w(f'- NEW unmapped groups observed: {unmapped} -> add to §9.2.')
    w('')

    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='Analyze a RadiaCode BLE soak log')
    ap.add_argument('logfile', help='path to soak_<ts>.jsonl(.gz)')
    ap.add_argument('--out', default=None, help='report path (default: alongside log as report_<name>.md)')
    args = ap.parse_args()

    path = Path(args.logfile)
    report = analyze(path)
    print(report)

    out = Path(args.out) if args.out else path.with_name(
        'report_' + path.name.replace('.jsonl.gz', '').replace('.jsonl', '') + '.md')
    out.write_text(report, encoding='utf-8')
    print(f'\n[analyze] report written -> {out}')


if __name__ == '__main__':
    main()
