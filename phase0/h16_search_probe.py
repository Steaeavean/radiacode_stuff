#!/usr/bin/env python3
"""H16 probe — найти быстрый/отзывчивый канал count_rate/dose_rate по BLE.

Фаза 0 показала: под обычным поллингом прибор отдаёт только сильно усреднённый
`RealTimeData` (за 6 ч cps 7.8–9.7, плоский), а сырой/быстрый канал
(`RawData`/`RawCountRate`/`RawDoseRate`) практически не доставляется — это блокирует
«Searching» (быстрый отклик при поиске источника). Этот probe помогает выяснить, можно ли
получить отзывчивый канал по BLE (протокол §14.10, H16).

Два режима:
  * по умолчанию (READ-ONLY, безопасно): быстрый поллинг `DATA_BUF`; в объявленных окнах
    двигайте прибор к источнику и от него; живой вывод по каналам показывает, какой канал
    реагирует (разброс cps в окне) — `RealTimeData` vs `RawData`/`RawCountRate`/`RawDoseRate`.
  * `--try-modes` (ПИШЕТ в прибор, с сохранением+восстановлением): дополнительно перебирает
    `MS_MODE`/`MS_SUB_MODE`/`CPS_FILTER`/`RAW_FILTER`. Исходные значения читаются «сырьём»
    в начале и восстанавливаются в конце (в т.ч. при Ctrl-C). Если прибор отвергает запись —
    логируется и идём дальше.

Запуск (без sudo; нужно разрешение Bluetooth для терминала):
  cd radiacode_stuff/phase0
  uv run python h16_search_probe.py                # безопасный read-only
  uv run python h16_search_probe.py --try-modes    # + перебор режимов (write+restore)

Двигайте прибор к источнику/от источника во время каждого окна «MOVE NOW».
Лог -> soak_logs/h16_<ts>.jsonl.gz.
"""

import argparse
import asyncio
import struct
import time

from bleak import BleakClient

from ble_transport import RadiaCodeBLE
from ble_soak import SoakLogger, adv_to_dict, cprint, scan_matches  # reuse helpers
from rc_reparse import reparse_records  # robust (0,4)-safe decoder
from radiacode.types import COMMAND, VSFR

FAST_CHANNELS = ('RealTimeData', 'RawData', 'RawCountRate', 'RawDoseRate', 'DoseRateDB')

# H17 confound 2x2 (+ stable-elevated) matrix: each cell = (label, poll_s, instruction).
# Read-only. Disentangles "is abundant RawData driven by poll-rate or by field activity?"
MATRIX_CELLS = [
    ('A_move_poll1.0', 1.0, 'ДВИГАЙТЕ прибор к источнику/от источника'),
    ('B_still_poll0.5', 0.5, 'ДЕРЖИТЕ НЕПОДВИЖНО (фон, далеко от источника)'),
    ('C_move_poll0.5', 0.5, 'ДВИГАЙТЕ прибор к источнику/от источника'),
    ('D_still_poll1.0', 1.0, 'ДЕРЖИТЕ НЕПОДВИЖНО (фон)'),
    ('E_hold_elevated_poll0.5', 0.5, 'ДЕРЖИТЕ НЕПОДВИЖНО ВПЛОТНУЮ к источнику (стабильно высокое поле)'),
]


async def read_vsfr_raw(rc: RadiaCodeBLE, vsfr) -> int | None:
    """Read a single VSFR as the raw uint32 (round-trip safe for save/restore)."""
    r = await rc.execute(COMMAND.RD_VIRT_SFR_BATCH, struct.pack('<II', 1, int(vsfr)))
    valid = r.unpack('<I')[0]
    if not (valid & 1) or r.size() < 4:
        return None
    return r.unpack('<I')[0]


async def fast_poll_window(rc, logger, label, seconds, poll, instruction='ДВИГАЙТЕ прибор к источнику/от источника'):
    cprint(f'=== {label}: {instruction} | {seconds:.0f}s, poll={poll}s ===')
    t0 = time.monotonic()
    seen = {}
    last_print = 0.0
    while time.monotonic() - t0 < seconds:
        try:
            raw, _ = await rc.data_buf()
        except Exception as ex:
            cprint(f'  poll err: {ex}')
            logger.log('error', where='h16_poll', label=label, msg=str(ex))
            break
        # robust (0,4)-safe reparse from raw — cdump's decoder desyncs on DoseCounter
        recs, meta = reparse_records(raw)
        if recs:
            logger.log('databuf', label=label, poll=poll, raw=raw.hex(), n=len(recs),
                       stop=meta['stop'], records=recs)
        for r in recs:
            cr = r.get('count_rate')
            if cr is not None:
                seen.setdefault(r['name'], []).append(cr)
        now = time.monotonic()
        if now - last_print >= 1.0:
            last_print = now
            parts = [f'{ch[:7]}={seen[ch][-1]:.1f}(n{len(seen[ch])})' for ch in FAST_CHANNELS if seen.get(ch)]
            cprint(f'  {label} {int(now - t0)}/{int(seconds)}s | ' + (' '.join(parts) if parts else 'нет записей'))
        await asyncio.sleep(poll)
    elapsed = max(time.monotonic() - t0, 1e-6)
    spreads = {ch: {'n': len(v), 'rate_per_s': round(len(v) / elapsed, 2),
                    'min': round(min(v), 2), 'max': round(max(v), 2),
                    'mean': round(sum(v) / len(v), 2), 'spread': round(max(v) - min(v), 2)}
               for ch, v in seen.items() if v}
    logger.log('window_summary', label=label, poll=poll, seconds=round(elapsed, 1), spreads=spreads)
    cprint(f'  -> {label} итог (rate_per_s = частота доставки; spread = отзывчивость):')
    for ch, st in spreads.items():
        cprint(f'     {ch}: n={st["n"]} rate={st["rate_per_s"]}/s min={st["min"]} '
               f'max={st["max"]} mean={st["mean"]} spread={st["spread"]}')
    if not spreads:
        cprint('     (каналов не пришло)')


def parse_args():
    ap = argparse.ArgumentParser(description='H16 fast-channel probe for RadiaCode (BLE)')
    ap.add_argument('--window', type=float, default=40.0, help='длительность одного окна, с (default 40)')
    ap.add_argument('--poll', type=float, default=0.5, help='интервал поллинга, с (default 0.5)')
    ap.add_argument('--scan-timeout', type=float, default=12.0)
    ap.add_argument('--device-name', default='RadiaCode/RC-')
    ap.add_argument('--logdir', default='soak_logs')
    ap.add_argument('--try-modes', action='store_true',
                    help='ПИСАТЬ MS_MODE/MS_SUB_MODE/CPS_FILTER/RAW_FILTER (с restore) — иначе read-only')
    ap.add_argument('--matrix', action='store_true',
                    help='H17 confound 2x2 (+stable-elevated) read-only: poll-rate x движение (см. MATRIX_CELLS)')
    return ap.parse_args()


# (VSFR, [values to try]) — exploratory; rejected writes are logged and skipped.
SWEEP_PLAN = [
    (VSFR.MS_MODE, [0, 1, 2, 3]),
    (VSFR.MS_SUB_MODE, [0, 1, 2, 3]),
    (VSFR.CPS_FILTER, [0, 1]),
    (VSFR.RAW_FILTER, [0, 1]),
]
SWEEP_VSFRS = [VSFR.MS_MODE, VSFR.MS_SUB_MODE, VSFR.CPS_FILTER, VSFR.RAW_FILTER]


async def amain():
    args = parse_args()
    import datetime
    from pathlib import Path
    Path(args.logdir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    logger = SoakLogger(Path(args.logdir) / f'h16_{stamp}.jsonl.gz')
    cprint(f'[h16] log -> {logger.path}  (try_modes={args.try_modes})')

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
    rc = RadiaCodeBLE(client, raw_logger=lambda d, rt, sq, p: logger.log('frame', dir=d, reqtype=f'0x{rt:04x}', seq=f'0x{sq:02x}', hex=p.hex()))
    await rc.start_notifications()
    await rc.init_session()
    cprint(f'CONNECTED {await rc.serial_number()} fw={(await rc.fw_version())[1]} mtu={getattr(client, "mtu_size", None)}')

    saved = {}
    try:
        if args.matrix:
            # H17 confound: read-only 2x2 (+ stable-elevated). Pause between cells so
            # the user can set up the motion/geometry for each labeled window.
            cprint('[h16] MATRIX (H17): read-only; следуйте инструкции каждого окна. '
                   'Сравните rate_per_s RawData между move/still и poll 1.0/0.5.')
            for label, poll, instruction in MATRIX_CELLS:
                cprint(f'--- следующее окно: {label} ({instruction}). Старт через 5 c ---')
                await asyncio.sleep(5.0)
                await fast_poll_window(rc, logger, label, args.window, poll, instruction)
            return

        # baseline (current settings) — always
        await fast_poll_window(rc, logger, 'baseline(current settings)', args.window, args.poll)

        if args.try_modes:
            cprint('[h16] saving MS_MODE/MS_SUB_MODE/CPS_FILTER/RAW_FILTER for restore ...')
            for v in SWEEP_VSFRS:
                saved[v] = await read_vsfr_raw(rc, v)
                cprint(f'   saved {v.name} = {saved[v]}')
            logger.log('saved_vsfr', values={v.name: saved[v] for v in SWEEP_VSFRS})

            for vsfr, values in SWEEP_PLAN:
                for val in values:
                    label = f'{vsfr.name}={val}'
                    try:
                        await rc.write_request(vsfr, struct.pack('<I', val))
                    except Exception as ex:
                        cprint(f'[h16] write {label} rejected: {ex} (skip)')
                        logger.log('write_rejected', vsfr=vsfr.name, value=val, msg=str(ex))
                        continue
                    logger.log('write_ok', vsfr=vsfr.name, value=val)
                    await fast_poll_window(rc, logger, label, args.window, args.poll)
    finally:
        if saved:
            cprint('[h16] restoring original VSFR values ...')
            for v, raw in saved.items():
                if raw is None:
                    continue
                try:
                    await rc.write_request(v, struct.pack('<I', raw))
                    cprint(f'   restored {v.name} = {raw}')
                except Exception as ex:
                    cprint(f'   restore {v.name} FAILED: {ex}')
                    logger.log('error', where='restore', vsfr=v.name, msg=str(ex))
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.close()
        cprint(f'[h16] done -> {logger.path}')
        cprint('[h16] смотри window_summary: какой канал имеет наибольший spread = самый отзывчивый.')


if __name__ == '__main__':
    asyncio.run(amain())
