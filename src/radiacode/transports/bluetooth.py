"""Bluetooth transports for RadiaCode devices.

Two backends are provided:

BluepyBluetooth  — Linux only, synchronous poll via bluepy.
BluetoothBleak   — macOS / Windows (and Linux fallback), async bleak wrapped
                   in a sync facade so the public RadiaCode API is unchanged.

``Bluetooth`` is a legacy alias for ``BluepyBluetooth`` kept for import
compatibility; prefer the named classes when selecting programmatically.
"""

import asyncio
import struct
import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from radiacode.bytes_buffer import BytesBuffer


# GATT profile (docs/radiacode-ble-protocol.md §2)
_SERVICE_UUID = 'e63215e5-7003-49d8-96b0-b024798fb901'
_WRITE_UUID = 'e63215e6-7003-49d8-96b0-b024798fb901'
_NOTIFY_UUID = 'e63215e7-7003-49d8-96b0-b024798fb901'


class DeviceNotFound(Exception):
    pass


class ConnectionClosed(Exception):
    pass


# ──────────────────────────────────────────────────────────────────
# BluetoothBleak — async bleak wrapped in a sync facade (macOS/Win)
# ──────────────────────────────────────────────────────────────────


class BluetoothBleak:
    """Synchronous BLE transport backed by bleak (macOS, Windows, non-Linux).

    A dedicated daemon thread runs an asyncio event loop. Every call to
    ``execute()`` schedules the async GATT write+notify roundtrip on that loop
    via ``run_coroutine_threadsafe()`` and blocks until the result is ready —
    keeping the synchronous ``RadiaCode`` public API intact.

    Wire framing (radiacode-ble-protocol.md §3):
      - Writes: 4-byte LE length-prefix + payload, sliced into ≤18 B packets.
      - Reads: notification stream reassembled into one payload (strips prefix).

    Device identification on macOS (CoreBluetooth does not expose MAC addresses):
      - ``address`` — CoreBluetooth UUID (system-assigned; use ``bleak`` scan to
        discover it once, then cache for subsequent connections).
      - ``name``    — name prefix used as a scan filter (e.g. ``"RadiaCode"``);
        first matching device is used.
      - Neither    — auto-scan: first device that advertises the RadiaCode service
        UUID *or* whose name starts with ``"RadiaCode"`` is used.
    """

    def __init__(
        self,
        address: Optional[str] = None,
        name: Optional[str] = None,
        scan_timeout: float = 10.0,
        connect_timeout: float = 20.0,
    ):
        self._loop = asyncio.new_event_loop()
        self._ble_thread = threading.Thread(
            target=self._loop.run_forever,
            name='radiacode-ble',
            daemon=True,
        )
        self._ble_thread.start()

        # Notification reassembler — only mutated from the loop thread
        self._resp_buf: bytes = b''
        self._resp_size: int = 0
        self._resp_future: Optional[asyncio.Future] = None

        try:
            self._run(
                self._connect(address, name, scan_timeout),
                timeout=connect_timeout,
            )
        except Exception as exc:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._ble_thread.join(timeout=5)
            raise DeviceNotFound(f'RadiaCode BLE device not found: {exc}') from exc

    # ── internal helpers ──────────────────────────────────────────

    def _run(self, coro, timeout: float = 30.0):
        """Schedule *coro* on the dedicated event loop and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _scan_device(self, name_prefix: Optional[str], scan_timeout: float):
        """Return a BLEDevice matching service UUID *or* name prefix."""
        from bleak import BleakScanner

        seen = await BleakScanner.discover(timeout=scan_timeout, return_adv=True)
        svc_lower = _SERVICE_UUID.lower()
        prefix = name_prefix or 'RadiaCode'

        # Pass 1 — prefer service-UUID match (most reliable)
        for _addr, (device, adv) in seen.items():
            adv_uuids = [u.lower() for u in (adv.service_uuids or [])]
            dev_name = adv.local_name or device.name or ''
            if svc_lower in adv_uuids or dev_name.startswith(prefix):
                return device

        raise DeviceNotFound(
            f'No RadiaCode device found in BLE scan '
            f'(looking for service UUID or name prefix "{prefix}"). '
            'Make sure Bluetooth is enabled and the device is nearby.'
        )

    async def _connect(
        self,
        address: Optional[str],
        name: Optional[str],
        scan_timeout: float,
    ) -> None:
        from bleak import BleakClient

        if address:
            target = address
        else:
            target = await self._scan_device(name, scan_timeout)

        self._client: BleakClient = BleakClient(target)
        await self._client.connect()
        # asyncio.Lock must be created inside the running event loop
        self._lock: asyncio.Lock = asyncio.Lock()
        await self._client.start_notify(_NOTIFY_UUID, self._on_notify)

    # ── notification reassembler (port of bluepy handleNotification, §3.2) ──

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        chunk = bytes(data)
        if self._resp_size == 0:
            if len(chunk) < 4:
                return  # malformed first fragment — ignore
            self._resp_size = 4 + struct.unpack('<i', chunk[:4])[0]
            self._resp_buf = chunk[4:]
        else:
            self._resp_buf += chunk
        self._resp_size -= len(chunk)
        if self._resp_size <= 0:
            payload = self._resp_buf
            self._resp_buf = b''
            self._resp_size = 0
            fut = self._resp_future
            if fut is not None and not fut.done():
                fut.set_result(payload)

    # ── command execution ─────────────────────────────────────────

    async def _execute_async(self, req: bytes) -> 'BytesBuffer':
        from radiacode.bytes_buffer import BytesBuffer

        async with self._lock:
            # Arm reassembler BEFORE writing so no notification is missed
            self._resp_buf = b''
            self._resp_size = 0
            self._resp_future = self._loop.create_future()
            try:
                # §3.1: slice request into ≤18-byte writes
                for pos in range(0, len(req), 18):
                    await self._client.write_gatt_char(_WRITE_UUID, req[pos : pos + 18], response=False)
                try:
                    payload = await asyncio.wait_for(self._resp_future, timeout=10.0)
                except asyncio.TimeoutError as exc:
                    raise ConnectionClosed('BLE response timeout') from exc
            except ConnectionClosed:
                raise
            except Exception as exc:
                raise ConnectionClosed(f'BLE error during execute: {exc}') from exc
            finally:
                self._resp_future = None

            return BytesBuffer(payload)

    def execute(self, req: bytes) -> 'BytesBuffer':
        if not self._ble_thread.is_alive():
            raise ConnectionClosed('BLE event-loop thread is not running')
        try:
            return self._run(self._execute_async(req))
        except ConnectionClosed:
            raise
        except Exception as exc:
            raise ConnectionClosed(f'BLE execute failed: {exc}') from exc

    def close(self) -> None:
        try:
            if hasattr(self, '_client') and self._client.is_connected:
                self._run(self._client.disconnect(), timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._ble_thread.join(timeout=5)


# ──────────────────────────────────────────────────────────────────
# BluepyBluetooth — Linux only, sync poll loop via bluepy
# ──────────────────────────────────────────────────────────────────

_have_bluepy = True
try:
    from bluepy.btle import BTLEDisconnectError, DefaultDelegate, Peripheral  # type: ignore[import-not-found]
except ImportError:
    _have_bluepy = False


if _have_bluepy:
    from radiacode.bytes_buffer import BytesBuffer as _BytesBuffer

    class BluepyBluetooth(DefaultDelegate):  # type: ignore[misc]
        """Synchronous BLE transport using bluepy (Linux only)."""

        def __init__(self, mac: str, poll_interval: float = 0.01):
            self._resp_buffer = b''
            self._resp_size = 0
            self._response = None
            self._closing = False
            self._poll_interval = poll_interval

            try:
                self.p = Peripheral(mac)
            except BTLEDisconnectError as exc:
                raise DeviceNotFound('Device not found or Bluetooth adapter is not powered on') from exc

            self.p.withDelegate(self)

            service = self.p.getServiceByUUID('e63215e5-7003-49d8-96b0-b024798fb901')
            self.write_fd = service.getCharacteristics('e63215e6-7003-49d8-96b0-b024798fb901')[0].getHandle()
            notify_fd = service.getCharacteristics('e63215e7-7003-49d8-96b0-b024798fb901')[0].getHandle()
            self.p.writeCharacteristic(notify_fd + 1, b'\x01\x00')

        def handleNotification(self, chandle, data):
            if self._resp_size == 0:
                self._resp_size = 4 + struct.unpack('<i', data[:4])[0]
                self._resp_buffer = data[4:]
            else:
                self._resp_buffer += data
            self._resp_size -= len(data)
            assert self._resp_size >= 0
            if self._resp_size == 0:
                self._response = self._resp_buffer
                self._resp_buffer = b''

        def execute(self, req) -> '_BytesBuffer':
            if self._closing:
                raise ConnectionClosed('Connection is closing')

            for pos in range(0, len(req), 18):
                self.p.writeCharacteristic(self.write_fd, req[pos : min(pos + 18, len(req))])

            timeout_end = time.time() + 10.0
            while self._response is None and not self._closing:
                remaining = timeout_end - time.time()
                if remaining <= 0:
                    raise TimeoutError('Response timeout')
                try:
                    self.p.waitForNotifications(min(self._poll_interval, remaining))
                except BTLEDisconnectError as err:
                    raise ConnectionClosed('Bluetooth connection lost') from err

            if self._closing:
                raise ConnectionClosed('Connection closed while waiting for response')

            br = _BytesBuffer(self._response)
            self._response = None
            return br

        def close(self) -> None:
            self._closing = True
            time.sleep(0.1)
            if hasattr(self, 'p') and self.p is not None:
                try:
                    self.p.disconnect()
                except Exception:
                    pass
                self.p = None

else:

    class BluepyBluetooth:  # type: ignore[no-redef]
        """Stub: bluepy is not installed; BLE via bluepy requires Linux + bluepy."""

        def __init__(self, mac: str, poll_interval: float = 0.01):
            raise DeviceNotFound(
                'bluepy is not installed. '
                'Bluetooth via bluepy is only supported on Linux with bluepy installed. '
                'On macOS/Windows use bluetooth_address or bluetooth_name with bleak.'
            )

        def execute(self, req: bytes) -> 'BytesBuffer':
            raise ConnectionClosed('BluepyBluetooth not available (bluepy not installed)')

        def close(self) -> None:
            pass


# Legacy alias — keep for any third-party code that imports ``Bluetooth``
Bluetooth = BluepyBluetooth
