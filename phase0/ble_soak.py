#!/usr/bin/env python3
"""RadiaCode BLE soak logger (Phase 0 validation, macOS / bleak).

Multi-hour structured logger of the live BLE exchange with a RadiaCode-10x,
emulating real conditions: continuous 1 s polling interleaved with random idle
phases (app backgrounded, connection alive) and random reconnects (BLE drops).

Closes the remaining Phase 0 hypotheses with real data:
  - H1/H2 advertised service UUID + name (captured on every (re)scan),
  - H3 negotiated MTU, H4 CCCD notify,
  - H8 (BLE) multi-fragment write chunking + notification reassembly (by construction),
  - H15 reconnect behaviour (RareData dose/duration before/after, via post-connect drain),
plus measures open numbers (RareData cadence, charge/temperature drift, ts wrap).

Output: gzip JSONL at soak_logs/soak_<ISO>.jsonl.gz (one event per line). See
analyze_soak.py for the offline report.

Run (macOS, NO sudo for BLE — only Bluetooth permission for the terminal):
  cd radiacode_stuff/phase0
  caffeinate -is uv run python ble_soak.py --hours 6

Defaults match the agreed profile: poll 1 s, idle 5-12 min (random), reconnect
15-45 min (random), spectrum every ~12 min.
"""

import argparse
import asyncio
import datetime
import gzip
import json
import random
import signal
import time
import traceback
from enum import Enum
from pathlib import Path

from bleak import BleakClient, BleakScanner

from ble_transport import (
    SERVICE_UUID,
    RadiaCodeBLE,
    record_to_dict,
)
from radiacode.types import VSFR

# VSFRs probed for the availability matrix (confirms CPS/DR/DS absent on RC-110,
# TEMP present, etc.). Names are logged via .name.
VSFR_PROBE_LIST = [
    VSFR.CPS,
    VSFR.DR_uR_h,
    VSFR.DS_uR,
    VSFR.TEMP_degC,
    VSFR.RAW_TEMP_degC,
    VSFR.USE_nSv_h,
    VSFR.DS_UNITS,
    VSFR.CR_UNITS,
    VSFR.DEVICE_TIME,
    VSFR.DISP_BRT,
    VSFR.SYS_TARGET_VERSION,
    VSFR.SYS_BOOT_VERSION,
    VSFR.ACC_X,
]


def _json_default(o):
    if isinstance(o, datetime.datetime):
        return o.isoformat()
    if isinstance(o, datetime.timedelta):
        return o.total_seconds()
    if isinstance(o, (bytes, bytearray)):
        return bytes(o).hex()
    if isinstance(o, Enum):
        return o.name
    return str(o)


def cprint(msg: str) -> None:
    """Timestamped console line (flushed) so a long run visibly progresses."""
    print(f'[{datetime.datetime.now():%H:%M:%S}] {msg}', flush=True)


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return f'{h}h{m:02d}m'
    if m:
        return f'{m}m{sec:02d}s'
    return f'{sec}s'


class SoakLogger:
    def __init__(self, path: Path):
        self._f = gzip.open(path, 'at', encoding='utf-8')
        self._t0 = time.monotonic()
        self.path = path

    def log(self, ev: str, **fields) -> None:
        rec = {
            't': datetime.datetime.now().isoformat(timespec='milliseconds'),
            'mono': round(time.monotonic() - self._t0, 3),
            'ev': ev,
        }
        rec.update(fields)
        self._f.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + '\n')
        self._f.flush()

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


async def scan_matches(name_prefixes, timeout: float):
    """Return list of (BLEDevice, AdvertisementData) for matching RadiaCode units."""
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    svc = SERVICE_UUID.lower()
    for _addr, (dev, adv) in seen.items():
        name = (adv.local_name or dev.name or '')
        adv_uuids = [u.lower() for u in (adv.service_uuids or [])]
        if any(name.startswith(p) for p in name_prefixes) or svc in adv_uuids:
            out.append((dev, adv))
    return out


def adv_to_dict(dev, adv) -> dict:
    return {
        'name': adv.local_name or dev.name,
        'address': dev.address,
        'rssi': adv.rssi,
        'service_uuids': list(adv.service_uuids or []),
        'service_uuid_advertised': SERVICE_UUID.lower() in [u.lower() for u in (adv.service_uuids or [])],
        'manufacturer_data': {str(k): bytes(v).hex() for k, v in (adv.manufacturer_data or {}).items()},
    }


class Soak:
    def __init__(self, args, logger: SoakLogger):
        self.args = args
        self.log = logger.log
        self.stop = asyncio.Event()
        self.client: BleakClient | None = None
        self.rc: RadiaCodeBLE | None = None
        self._reconnect_count = 0
        # ---- live status (for console heartbeat) ----
        self.connected = False
        self.device_addr = None
        self.serial = None
        self.fw_str = None
        self.mtu = None
        self.last_rt = None        # (count_rate cps, dose_rate R/h)
        self.last_rare = None      # (dose R, duration s, temp C, charge frac)
        self.total_records = 0
        self.state = 'init'
        self.start_mono = time.monotonic()
        self.deadline = self.start_mono + args.hours * 3600.0
        self.phase_until = 0.0
        self.next_reconnect = 0.0
        self._last_status = 0.0

    def _ingest(self, records) -> None:
        """Update live counters from a freshly decoded DATA_BUF batch."""
        for r in records:
            self.total_records += 1
            name = type(r).__name__
            if name == 'RealTimeData':
                self.last_rt = (r.count_rate, r.dose_rate)
            elif name == 'RareData':
                self.last_rare = (r.dose, r.duration, r.temperature, r.charge_level)

    def _status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_status < self.args.status_every:
            return
        self._last_status = now
        conn = 'CONN' if self.connected else 'DISC'
        rt = '--'
        if self.last_rt:
            cps, dr = self.last_rt
            rt = f'cps={cps:.1f} dr={dr * 1e4:.3f}uSv/h'
        rare = ''
        if self.last_rare:
            dose, _dur, temp, chg = self.last_rare
            rare = f' rare(dose={dose:.3g}R chg={(chg or 0) * 100:.0f}% t={temp:.1f}C)'
        phase = self.state
        if self.state == 'idle':
            phase = f'idle(resume {hms(self.phase_until - now)})'
        cprint(f'{conn} {phase} | {rt} | rec={self.total_records}{rare} | '
               f't+{hms(now - self.start_mono)}/{hms(self.args.hours * 3600)} '
               f'next_rc={hms(self.next_reconnect - now)}')

    # ----- connection lifecycle -----
    async def connect(self, tag: str) -> bool:
        """Scan, connect, init session, snapshot identity + VSFR matrix + post-connect drain."""
        prefixes = [p for p in self.args.device_name.split('/') if p]
        cprint(f'scanning for {"/".join(prefixes)} (timeout {self.args.scan_timeout:.0f}s) ...')
        matches = await scan_matches(prefixes, self.args.scan_timeout)
        for dev, adv in matches:
            self.log('adv', tag=tag, **adv_to_dict(dev, adv))
            cprint(f'found "{adv.local_name or dev.name}" addr={dev.address} rssi={adv.rssi} '
                   f'svc_adv={"yes" if SERVICE_UUID.lower() in [u.lower() for u in (adv.service_uuids or [])] else "no"}')
        if not matches:
            self.log('error', where='scan', msg='no RadiaCode found', prefixes=prefixes)
            cprint(f'no device found (will retry) [{tag}]')
            return False

        dev, _adv = matches[0]
        cprint(f'connecting to {dev.address} ...')
        client = BleakClient(dev, disconnected_callback=self._on_disconnect, timeout=30.0)
        await client.connect()
        rc = RadiaCodeBLE(client, raw_logger=self._raw_logger if self.args.raw_frames else None)
        await rc.start_notifications()
        cfg = await rc.init_session()

        self.client = client
        self.rc = rc

        serial = await rc.serial_number()
        fw = await rc.fw_version()
        fw_sig = await rc.fw_signature()
        self.connected = True
        self.device_addr = dev.address
        self.serial = serial
        self.fw_str = f'{fw[1][0]}.{fw[1][1]}'
        self.mtu = getattr(client, 'mtu_size', None)
        self.log(
            'session_start',
            tag=tag,
            serial=serial,
            fw_boot=list(fw[0]),
            fw_target=list(fw[1]),
            fw_signature=fw_sig,
            spec_format_version=rc.spectrum_format_version,
            mtu=self.mtu,
            config=cfg,
            args=vars(self.args),
        )
        cprint(f'CONNECTED serial={serial} fw={self.fw_str} mtu={self.mtu} addr={dev.address} [{tag}]')
        await self.vsfr_matrix(tag)
        # drain post-connect backlog (often contains a flushed RareData -> H15 signal)
        n = await self.drain(reason=f'post_connect:{tag}')
        cprint(f'post-connect drain: {n} records'
               + (f' | latest cps={self.last_rt[0]:.1f} dr={self.last_rt[1] * 1e4:.3f}uSv/h' if self.last_rt else ''))
        return True

    def _on_disconnect(self, _client) -> None:
        self.connected = False
        self.log('disconnect', reason='callback')
        cprint('disconnected (callback)')

    def _raw_logger(self, direction: str, reqtype: int, seq: int, payload: bytes) -> None:
        self.log('frame', dir=direction, reqtype=f'0x{reqtype:04x}', seq=f'0x{seq:02x}', hex=payload.hex())

    async def safe_disconnect(self, reason: str) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception as ex:
                self.log('error', where='disconnect', msg=str(ex))
        self.log('disconnect', reason=reason)
        self.connected = False
        self.client = None
        self.rc = None

    # ----- reads -----
    async def vsfr_matrix(self, tag: str) -> None:
        matrix = {}
        for vsfr in VSFR_PROBE_LIST:
            try:
                ok, value = await self.rc.probe_vsfr(vsfr)
                matrix[vsfr.name] = {'ok': ok, 'value': value}
            except Exception as ex:
                matrix[vsfr.name] = {'ok': False, 'error': str(ex)}
        self.log('vsfr_probe', tag=tag, matrix=matrix)

    async def poll_once(self) -> None:
        raw, records = await self.rc.data_buf()
        self.log('databuf', raw=raw.hex(), n=len(records), records=[record_to_dict(r) for r in records])
        self._ingest(records)
        self._status()

    async def drain(self, reason: str, max_reads: int = 40) -> int:
        """Read DATA_BUF until two consecutive empty reads (or cap). Logs each batch."""
        total = 0
        empties = 0
        for _ in range(max_reads):
            try:
                raw, records = await self.rc.data_buf()
            except Exception as ex:
                self.log('error', where=f'drain:{reason}', msg=str(ex))
                break
            self.log('databuf', drain=reason, raw=raw.hex(), n=len(records),
                     records=[record_to_dict(r) for r in records])
            self._ingest(records)
            total += len(records)
            if records:
                empties = 0
            else:
                empties += 1
                if empties >= 2:
                    break
                await asyncio.sleep(0.2)
        return total

    async def read_spectrum(self) -> None:
        raw, spec = await self.rc.spectrum()
        total = int(sum(spec.counts))
        self.log('spectrum', raw=raw.hex(), duration_s=spec.duration.total_seconds(),
                 a0=spec.a0, a1=spec.a1, a2=spec.a2, n_channels=len(spec.counts),
                 counts_sum=total, counts=spec.counts)
        cprint(f'spectrum: {len(spec.counts)} ch, dur={hms(spec.duration.total_seconds())}, sum={total}')

    # ----- main loop -----
    async def reconnect(self) -> None:
        self._reconnect_count += 1
        n = self._reconnect_count
        self.log('reconnect', phase='begin', n=n, gap_s=self.args.reconnect_gap)
        cprint(f'== reconnect #{n}: disconnect, wait {self.args.reconnect_gap:.0f}s, rescan ==')
        await self.safe_disconnect(reason=f'reconnect#{n}')
        await asyncio.sleep(self.args.reconnect_gap)
        # retry connect with backoff until success or stop
        backoff = 2.0
        while not self.stop.is_set():
            try:
                if await self.connect(tag=f'reconnect#{n}'):
                    self.log('reconnect', phase='end', n=n)
                    return
            except Exception as ex:
                self.log('error', where=f'reconnect#{n}', msg=str(ex), trace=traceback.format_exc())
                cprint(f'reconnect #{n} attempt failed: {ex} (retry in {min(backoff, 30.0):.0f}s)')
            await asyncio.sleep(min(backoff, 30.0))
            backoff *= 1.5

    async def run(self) -> None:
        self.start_mono = time.monotonic()
        self.deadline = self.start_mono + self.args.hours * 3600.0
        cprint(f'soak start: {hms(self.args.hours * 3600)} run, poll {self.args.poll:.0f}s, '
               f'idle {self.args.idle_min:.0f}-{self.args.idle_max:.0f}s, '
               f'reconnect {self.args.reconnect_min:.0f}-{self.args.reconnect_max:.0f}s')

        # initial connect (retry until success or stop)
        while not self.stop.is_set():
            try:
                if await self.connect(tag='initial'):
                    break
            except Exception as ex:
                self.log('error', where='initial_connect', msg=str(ex), trace=traceback.format_exc())
                cprint(f'initial connect failed: {ex} (retry in 5s)')
            await self._sleep(5.0)

        now = time.monotonic()
        self.state = 'poll'
        self.phase_until = now + random.uniform(self.args.poll_min, self.args.poll_max)
        self.next_reconnect = now + random.uniform(self.args.reconnect_min, self.args.reconnect_max)
        next_spectrum = now + self.args.spectrum_every
        self.log('phase', state=self.state, until_in_s=round(self.phase_until - now, 1),
                 next_reconnect_in_s=round(self.next_reconnect - now, 1))
        cprint(f'polling (next idle in {hms(self.phase_until - now)}, next reconnect in {hms(self.next_reconnect - now)})')
        self._status(force=True)

        while not self.stop.is_set() and time.monotonic() < self.deadline:
            now = time.monotonic()

            # scheduled reconnect (overrides phase)
            if now >= self.next_reconnect:
                await self.reconnect()
                now = time.monotonic()
                self.next_reconnect = now + random.uniform(self.args.reconnect_min, self.args.reconnect_max)
                self.state = 'poll'
                self.phase_until = now + random.uniform(self.args.poll_min, self.args.poll_max)
                self.log('phase', state=self.state, after='reconnect',
                         next_reconnect_in_s=round(self.next_reconnect - now, 1))
                self._status(force=True)
                continue

            if self.rc is None:
                # lost connection outside scheduled reconnect -> recover
                cprint('connection lost -> recovering')
                await self.reconnect()
                continue

            if self.state == 'poll':
                try:
                    await self.poll_once()
                    if now >= next_spectrum and self.args.spectrum_every > 0:
                        await self.read_spectrum()
                        next_spectrum = now + self.args.spectrum_every
                except Exception as ex:
                    self.log('error', where='poll', msg=str(ex), trace=traceback.format_exc())
                    cprint(f'poll error: {ex} -> reconnect')
                    await self.safe_disconnect(reason='poll_error')
                    continue
                if now >= self.phase_until:
                    self.state = 'idle'
                    self.phase_until = now + random.uniform(self.args.idle_min, self.args.idle_max)
                    self.log('phase', state='idle', dur_s=round(self.phase_until - now, 1))
                    cprint(f'-> idle for {hms(self.phase_until - now)} (connection kept alive)')
                    self._status(force=True)
                await self._sleep(self.args.poll)
            else:  # idle: connection alive, no DATA_BUF reads (emulates background)
                if now >= self.phase_until:
                    self.state = 'poll'
                    self.phase_until = now + random.uniform(self.args.poll_min, self.args.poll_max)
                    self.log('phase', state='poll', after='idle', dur_s=round(self.phase_until - now, 1))
                    cprint(f'-> resume polling (drain catch-up); next idle in {hms(self.phase_until - now)}')
                    if self.rc is not None:
                        try:
                            await self.drain(reason='idle_resume')
                        except Exception as ex:
                            self.log('error', where='idle_resume_drain', msg=str(ex))
                else:
                    self._status()  # idle heartbeat (throttled)
                await self._sleep(1.0)

        self.log('session_end', reason='stop' if self.stop.is_set() else 'deadline',
                 reconnects=self._reconnect_count)
        cprint(f'session end ({"ctrl-c" if self.stop.is_set() else "deadline"}); '
               f'reconnects={self._reconnect_count}, records={self.total_records}')
        await self.safe_disconnect(reason='session_end')

    async def _sleep(self, seconds: float) -> None:
        """Sleep that wakes early on stop."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


def parse_args():
    ap = argparse.ArgumentParser(description='RadiaCode BLE soak logger (Phase 0)')
    ap.add_argument('--hours', type=float, default=6.0, help='total run duration (default 6)')
    ap.add_argument('--poll', type=float, default=1.0, help='DATA_BUF poll interval, s (default 1)')
    ap.add_argument('--poll-min', type=float, default=120.0, help='poll-phase min duration, s (default 120)')
    ap.add_argument('--poll-max', type=float, default=360.0, help='poll-phase max duration, s (default 360)')
    ap.add_argument('--idle-min', type=float, default=300.0, help='idle-phase min, s (default 300 = 5 min)')
    ap.add_argument('--idle-max', type=float, default=720.0, help='idle-phase max, s (default 720 = 12 min)')
    ap.add_argument('--reconnect-min', type=float, default=900.0, help='min interval between reconnects, s (default 900 = 15 min)')
    ap.add_argument('--reconnect-max', type=float, default=2700.0, help='max interval between reconnects, s (default 2700 = 45 min)')
    ap.add_argument('--reconnect-gap', type=float, default=10.0, help='disconnect->reconnect pause, s (default 10)')
    ap.add_argument('--spectrum-every', type=float, default=720.0, help='spectrum read interval, s; 0 disables (default 720 = 12 min)')
    ap.add_argument('--scan-timeout', type=float, default=12.0, help='BLE scan timeout, s (default 12)')
    ap.add_argument('--device-name', default='RadiaCode/RC-', help="name prefixes ('/'-separated) to match (default 'RadiaCode/RC-')")
    ap.add_argument('--logdir', default='soak_logs', help='output dir (default soak_logs)')
    ap.add_argument('--status-every', type=float, default=10.0, help='console status line interval, s (default 10)')
    ap.add_argument('--no-raw-frames', dest='raw_frames', action='store_false', help='do not log raw tx/rx frame hex (smaller log)')
    ap.set_defaults(raw_frames=True)
    return ap.parse_args()


async def amain():
    args = parse_args()
    logdir = Path(args.logdir)
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
    logger = SoakLogger(logdir / f'soak_{stamp}.jsonl.gz')
    print(f'[soak] logging to {logger.path}  (hours={args.hours})')

    soak = Soak(args, logger)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, soak.stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await soak.run()
    finally:
        logger.close()
        print(f'[soak] done -> {logger.path}')
        print(f'[soak] analyze: uv run python analyze_soak.py {logger.path}')


if __name__ == '__main__':
    asyncio.run(amain())
