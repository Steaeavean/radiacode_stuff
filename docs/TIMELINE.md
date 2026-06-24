# Repository Timeline — radiacode_stuff

**Rule:** Append a new dated entry here **only on explicit user request**
(e.g. "зафиксируй", "добавь в таймлайн", "запиши действия").
Do not auto-update on every change; this is a curated log, not a git log.

---

## 2026-06-24 — Initial fork + macOS BLE port

**Repo created / first release (0.4.0).**

### Baseline
- Forked [`cdump/radiacode`](https://github.com/cdump/radiacode) @`4704259`
  (v0.3.5) into `Steaeavean/radiacode_stuff`, `main` branch.
- Local working copy: `/Users/vadimkz/Projects/radiacode_stuff`

### Core change: BluetoothBleak transport
- Replaced Darwin stub in `src/radiacode/transports/bluetooth.py` with
  `BluetoothBleak` — async [bleak](https://github.com/hbldh/bleak) wrapped in
  a sync facade (daemon thread + asyncio event loop,
  `run_coroutine_threadsafe`).
- Notification reassembler (4-byte LE prefix + notification chunks), chunked
  write ≤ 18 B, `asyncio.Lock` for single-in-flight command — ported from
  `atomapp-ios/radiacode_stuff/phase0/ble_transport.py`.
- `BluepyBluetooth` (Linux) preserved unchanged. Legacy `Bluetooth` alias kept.
- `RadiaCode.__init__`: removed `_bt_supported = platform.system() != 'Darwin'`;
  added `bluetooth_address` and `bluetooth_name` params; routes to
  `BluetoothBleak` on non-Linux.

### Dependencies
- `pyproject.toml`: `bleak>=0.22 ; sys_platform != 'linux'`; `requires-python = ">=3.10"`.
- Version bumped `0.3.5` → `0.4.0`.
- `uv lock` regenerated.

### Examples
- All 5 example scripts (`basic`, `webserver`, `radiacode-exporter`,
  `narodmon`, `show-spectrum`) updated with `--bluetooth-address` /
  `--bluetooth-name` flags.
- `basic.py`: added `try/except KeyboardInterrupt` + `rc._connection.close()`,
  fixed connection-label operator-precedence bug.

### Phase 0 validation scripts
- Copied `atomapp-ios/radiacode_stuff/phase0/` → `phase0/` in repo.
- `phase0/pyproject.toml`: path-dep changed from absolute to `path = ".."` (in-repo).
- `.gitignore`: exclude `phase0/.venv`, `phase0/uv.lock`, `phase0/soak_logs/*.jsonl.gz`.

### timesync.py
- New standalone utility: connects via BLE, calls `set_local_time(now)`,
  reports drift from DATA_BUF timestamps.
- Flags: `--bluetooth-address`, `--bluetooth-name`, `--threshold`, `--dry-run`.
- Root cause documented: official RadiaCode Android app sets UTC; DATA_BUF
  timestamps are always system-correct (DEVICE_TIME elapsed, not RTC).

### Documentation
- `README.md`: bilingual header link, Utilities section, fork rationale (why
  bluepy is Linux-only, CoreBluetooth MAC limitation).
- `README.ru.md`: full Russian README (EN-parity).
- `docs/TIMESYNC.md`: bilingual (EN+RU) deep-dive on time sync.
- `docs/TIMELINE.md`: this file.
- `DEVICES.local.md` (gitignored): RC-101-005265, UUID `62B635D0-CFAA-1B4C-204F-D1837DEF3F68`.

### CI / Release
- `.github/workflows/build.yml` updated: `master` → `main`, Python matrix
  3.10+3.13, macOS+Linux runners, removed PyPI publish, fixed repo URL in
  release notes, added import smoke test.
- `phase0/` and `timesync.py` excluded from ruff lint.
- Ruff formatting applied to `src/`.
- Tag `0.4.0` pushed; GitHub Actions ran lint + 4 test-matrix jobs + release
  job in 53 s. GitHub Release 0.4.0 created with wheel + sdist artifacts.

### Hardware validation
- Smoke test: `uv run python -m radiacode.examples.basic --bluetooth-name RadiaCode` — RC-101-005265 fw 4.14, serial, spectrum (1024 ch), RealTimeData / RareData / DoseRateDB streaming. ✓
- `timesync.py`: drift -19 s (BLE latency + sleep(3)), SET_TIME applied. ✓
