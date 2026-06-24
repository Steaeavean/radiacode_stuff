"""Robust value-extracting permissive reparser for VS.DATA_BUF payloads.

WHY THIS EXISTS (and why we do NOT use cdump's decode_VS_DATA_BUF for analysis):
the reference decoder in `cdump/radiacode` treats group (0,4) as a 16-byte
`GRP_UserData` (`<IffHH`), but on RC-1xx fw4.14 it is really a **6-byte**
`GRP_DoseCounter` (`<IH` = uint32 uR + uint16 flags, docs §9.2 / §14.8). A single
(0,4) record therefore desynchronises cdump's walk and silently drops every record
after it in the same DATA_BUF read. The soak/probe logs store the *raw* payload
hex precisely so we can re-decode correctly here.

This module is the single source of truth for offline H18/H19 analysis and for the
live H17 matrix probe. It walks records by the known fixed sizes (docs §9.2),
decodes the fields we care about, and stops (like the firmware) on a seq jump,
truncation, or a genuinely unknown group — recording why, plus a raw sample.

Phase-0 throwaway validation tooling — NOT atomapp-ios production code.
"""

import struct
from math import gcd

# eid/gid -> (name, struct_fmt, field_names). Fixed-size groups only (after the
# 7-byte <BBBi record header). Sizes match docs/radiacode-ble-protocol.md §9.2.
FIXED_GROUPS = {
    (0, 0): ('RealTimeData', '<ffHHHB',
             ('count_rate', 'dose_rate', 'count_rate_err', 'dose_rate_err', 'flags', 'real_time_flags')),
    (0, 1): ('RawData', '<ff', ('count_rate', 'dose_rate')),
    (0, 2): ('DoseRateDB', '<IffHH',
             ('count', 'count_rate', 'dose_rate', 'dose_rate_err', 'flags')),
    (0, 3): ('RareData', '<IfHHH',
             ('duration', 'dose', 'temperature', 'charge_level', 'flags')),
    (0, 4): ('DoseCounter', '<IH', ('dose_counter', 'flags')),   # uR + flags (6 bytes!)
    (0, 6): ('AccelData', '<HHH', ('acc_x', 'acc_y', 'acc_z')),
    (0, 7): ('Event', '<BBH', ('event', 'event_param1', 'flags')),
    (0, 8): ('RawCountRate', '<fH', ('count_rate', 'flags')),
    (0, 9): ('RawDoseRate', '<fH', ('dose_rate', 'flags')),
}
# Groups whose layout we still do NOT trust enough to step over.
UNCERTAIN = {(0, 5)}  # SheduleData — unconfirmed


def _post_decode(name, d):
    """Apply the §9.3 unit corrections, leaving raw fields in place too."""
    if 'count_rate_err' in d:
        d['count_rate_err_pct'] = d['count_rate_err'] / 10.0
    if 'dose_rate_err' in d:
        d['dose_rate_err_pct'] = d['dose_rate_err'] / 10.0
    if name == 'RareData':
        d['temperature_c'] = (d['temperature'] - 2000) / 100.0
        d['charge_frac'] = d['charge_level'] / 100.0
        d['dose_nsv'] = d['dose'] * 1e7          # R -> nSv
    if name == 'DoseCounter':
        d['dose_counter_nsv'] = d['dose_counter'] * 10.0   # uR -> nSv
        d['flags_hex'] = f'0x{d["flags"]:04x}'
    if name in ('RealTimeData', 'RawData', 'DoseRateDB', 'RawDoseRate'):
        if 'dose_rate' in d:
            d['dose_rate_nsv_h'] = d['dose_rate'] * 1e7    # R/h -> nSv/h
    return d


def reparse_records(raw: bytes):
    """Walk a DATA_BUF payload, returning (records, meta).

    records: list of dicts with keys: seq, eid, gid, name, ts_offset, ts_ms,
             + decoded fields (+ unit-corrected aliases).
    meta:    {'stop': reason|None, 'leftover': int, 'n': int, 'unknown': [...]}.
    """
    records = []
    meta = {'stop': None, 'leftover': 0, 'n': 0, 'unknown': []}
    pos, n, prev_seq = 0, len(raw), None
    while n - pos >= 7:
        seq, eid, gid, ts_offset = struct.unpack_from('<BBBi', raw, pos)
        key = (eid, gid)
        if prev_seq is not None and (prev_seq + 1) % 256 != seq:
            meta['stop'] = 'seq_jump'
            break
        prev_seq = seq

        if key in UNCERTAIN:
            meta['unknown'].append({'key': key, 'reason': 'uncertain_layout',
                                    'after_header_hex': raw[pos + 7:pos + 47].hex()})
            meta['stop'] = 'uncertain_group'
            break

        if eid == 0 and key in FIXED_GROUPS:
            name, fmt, fields = FIXED_GROUPS[key]
            sz = struct.calcsize(fmt)
            if n - pos - 7 < sz:
                meta['stop'] = 'trunc'
                break
            vals = struct.unpack_from(fmt, raw, pos + 7)
            d = {'seq': seq, 'eid': eid, 'gid': gid, 'name': name,
                 'ts_offset': ts_offset, 'ts_ms': ts_offset * 10}
            d.update(dict(zip(fields, vals)))
            records.append(_post_decode(name, d))
            pos += 7 + sz
        elif eid == 1 and gid in (1, 2, 3):
            if n - pos - 7 < 6:
                meta['stop'] = 'trunc'
                break
            samples = int.from_bytes(raw[pos + 7:pos + 9], 'little')
            k = {1: 8, 2: 16, 3: 14}[gid]
            sz = 6 + k * samples
            if n - pos - 7 < sz:
                meta['stop'] = 'trunc'
                break
            records.append({'seq': seq, 'eid': eid, 'gid': gid,
                            'name': f'svc/{eid}.{gid}', 'ts_offset': ts_offset,
                            'ts_ms': ts_offset * 10, 'samples': samples})
            pos += 7 + sz
        else:
            meta['unknown'].append({'key': key, 'reason': 'unknown_group',
                                    'after_header_hex': raw[pos + 7:pos + 47].hex()})
            meta['stop'] = 'unknown_group'
            break

    meta['leftover'] = n - pos
    meta['n'] = len(records)
    return records, meta


def float_gcd(values, tol=1e-6):
    """Approximate gcd of a list of (near-)integer floats. Returns None if the
    values are not all near-integers (then the channel is genuinely fractional)."""
    ints = []
    for v in values:
        r = round(v)
        if abs(v - r) > tol:
            return None
        ints.append(int(r))
    g = 0
    for i in ints:
        g = gcd(g, abs(i))
    return g
