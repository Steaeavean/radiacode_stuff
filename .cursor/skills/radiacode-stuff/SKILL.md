---
name: radiacode-stuff
description: >-
  Python library fork of cdump/radiacode with full macOS BLE support via bleak.
  Use when working on the fork repo at /Users/vadimkz/Projects/radiacode_stuff:
  BluetoothBleak transport, timesync, phase0 scripts, CI/release workflow,
  or any changes to transports/bluetooth.py, radiacode.py, examples, or docs.
  Also read before touching cdump/radiacode upstream sync or protocol research.
---

# radiacode-stuff — Python library fork with macOS BLE

**Repo:** `Steaeavean/radiacode_stuff` (GitHub)
**Local path:** `/Users/vadimkz/Projects/radiacode_stuff`
**Branch:** `main`
**Upstream:** `cdump/radiacode` (remote `upstream`)
**Version:** `0.4.0` (initial fork release, 2026-06-24)
**Upstream base:** `cdump/radiacode@4704259` (0.3.5)

---

## Why this fork?

The upstream library uses [bluepy](https://github.com/IanHarvey/bluepy) for
BLE — a Linux-only library that calls into BlueZ D-Bus. On macOS upstream
raises `ConnectionClosed("Bluetooth is not supported on this platform")`.

**Fix:** Darwin stub replaced with
[bleak](https://github.com/hbldh/bleak) (CoreBluetooth/WinRT/BlueZ).
Since bleak is async and `RadiaCode` API is synchronous, bleak runs in a
dedicated daemon thread with its own asyncio loop; callers see no change.

Additional macOS constraint: **CoreBluetooth does not expose MAC addresses.**
Device discovery uses a BLE scan filtered by service UUID `e63215e5-…` or
name prefix, plus optional `bluetooth_address` for the CoreBluetooth-assigned
UUID.

Validated: 6-hour soak on RC-101 fw 4.14 (scripts in `phase0/`).

---

## Architecture

```
RadiaCode (sync public API)
  └─ __init__: platform check → selects transport
       ├─ Linux + bluetooth_mac   → BluepyBluetooth (bluepy, unchanged)
       ├─ bluetooth_address/name  → BluetoothBleak  (macOS/Win/Linux-bleak)
       └─ neither                 → Usb             (pyusb)
```

### BluetoothBleak  (`src/radiacode/transports/bluetooth.py`)

- `__init__(address, name, scan_timeout, connect_timeout)` — starts daemon
  thread + asyncio loop; runs `_connect(...)` blocking via
  `run_coroutine_threadsafe(...).result(timeout)`.
- `_scan_device(name_prefix, scan_timeout)` — `BleakScanner.discover()`,
  filtered by service UUID `e63215e5-…` or name prefix `RadiaCode`.
- `_on_notify(char, data)` — notification reassembler: 4-byte LE length
  prefix + payload chunking (port of phase0/ble_transport.py).
- `execute(req: bytes) → BytesBuffer` — chunks write by 18 B, waits for
  `asyncio.Future` set by `_on_notify`.
- `close()` — disconnect BleakClient, stop event loop, join thread.

### RadiaCode constructor additions

New params vs upstream:
- `bluetooth_address: str | None` — CoreBluetooth UUID (macOS) or BLE
  address (Windows/Linux). Takes priority over `bluetooth_name`.
- `bluetooth_name: str | None` — name prefix for auto-scan.

Removed: `self._bt_supported = platform.system() != 'Darwin'` gate.

---

## Key files

| Path | Purpose |
|---|---|
| `src/radiacode/transports/bluetooth.py` | `BluetoothBleak` + `BluepyBluetooth` + legacy `Bluetooth` alias |
| `src/radiacode/radiacode.py` | Main class, transport selection |
| `src/radiacode/examples/basic.py` | CLI with `--bluetooth-address/--bluetooth-name`; handles KeyboardInterrupt + closes BLE |
| `src/radiacode/examples/webserver.py` | Same BLE flags; aiohttp web UI |
| `timesync.py` | Sync device RTC to macOS local time (explicit SET_TIME) |
| `phase0/ble_transport.py` | Async bleak command layer (reference / research) |
| `phase0/ble_soak.py` | Multi-hour soak logger |
| `phase0/pyproject.toml` | `path = ".."` editable dep — runs against in-repo radiacode |
| `docs/TIMESYNC.md` | Bilingual (EN+RU) time sync deep-dive |
| `docs/TIMELINE.md` | Chronological log of repo changes — update on user request |
| `DEVICES.local.md` | Machine-local device UUIDs (gitignored) |
| `.github/workflows/build.yml` | CI: lint + test (macOS+Linux × 3.10+3.13) + GitHub Release on tag |

---

## Device on this machine

| Field | Value |
|---|---|
| Model | RC-101-005265 |
| CoreBluetooth UUID | `62B635D0-CFAA-1B4C-204F-D1837DEF3F68` |
| Firmware | 4.14 (Jul 7 2025) |

Quick connect:
```bash
cd /Users/vadimkz/Projects/radiacode_stuff
uv run python -m radiacode.examples.basic --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68
```

---

## timesync.py — RTC sync

Device has `SET_TIME` command but **no `GET_TIME`**. Library already calls
`set_local_time(now)` in every `RadiaCode.__init__`. Script makes this
explicit and confirms with a drift report from DATA_BUF timestamps.

Typical cause of drift: official RadiaCode Android app sets device to UTC.
DATA_BUF timestamps are always system-time-correct (use elapsed DEVICE_TIME
counter, not RTC) — only device *display* shows wrong time.

```bash
uv run python timesync.py --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68
uv run python timesync.py --dry-run
```

---

## pyproject.toml / deps

```toml
dependencies = [
    "bluepy~=1.3 ; sys_platform == 'linux'",
    "bleak>=0.22 ; sys_platform != 'linux'",
    "pyusb~=1.3",
]
requires-python = ">=3.10"
```

---

## CI workflow (`.github/workflows/build.yml`)

Triggers:
- `push` to `main` → lint + test matrix
- tag `[0-9]*.[0-9]*.[0-9]*` → lint + test + release

Jobs:
- **lint**: `ruff check` + `ruff format --check` (Ubuntu; `phase0/` and
  `timesync.py` excluded via `[tool.ruff] exclude`).
- **test**: macOS + Ubuntu × Python 3.10 + 3.13 → `uv sync` + import smoke.
- **release**: `uv build` + auto release notes from git log + `softprops/action-gh-release`.
  No PyPI publish (personal fork).

---

## Release process

```bash
# 1. Bump version in pyproject.toml
# 2. uv lock  (updates uv.lock)
# 3. git add + git commit
# 4. git tag -a X.Y.Z -m "Release X.Y.Z — <summary>"
# 5. git push origin main
# 6. git push origin X.Y.Z   ← triggers GitHub Actions release
```

---

## TIMELINE rule

**When user explicitly requests to record repository actions** (e.g. "зафиксируй",
"добавь в таймлайн", "запиши действия"), append a new dated entry to
`docs/TIMELINE.md`. Do not auto-update the timeline on every change —
only on explicit user request.

---

## Protocol reference

Full GATT/wire protocol (VSFR, VS, DATA_BUF decoder, framing, init sequence)
is documented in `atomapp-ios/docs/radiacode-ble-protocol.md` — the source
of truth for the wire format used by both the iOS ObjC stack and this Python
library.
