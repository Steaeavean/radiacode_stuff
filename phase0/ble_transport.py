"""Async BLE transport for RadiaCode over CoreBluetooth (bleak), macOS.

Reproduces the cdump/radiacode framing + command layer (transports/bluetooth.py +
radiacode.py::execute/read_request/...) on top of bleak, and REUSES the pure,
synchronous decoders + enums from the installed `radiacode` package
(decode_VS_DATA_BUF / decode_RC_VS_SPECTRUM / BytesBuffer / COMMAND / VSFR / VS).

Why a custom transport: cdump/radiacode's BLE backend is bluepy (Linux-only); on
macOS its Bluetooth class is an empty stub. The application/command layer is
transport-identical (see docs/radiacode-ble-protocol.md §1, §3), so everything
above the wire is reused unchanged.

Protocol references (docs/radiacode-ble-protocol.md):
  §2 GATT profile, §3 framing (4-byte length prefix + 18-byte chunking +
  notification reassembler), §4 command layer (seq 0x80+n%32, echo header),
  §6 session init, §8.1 RD_VIRT_STRING + 0x00 tail hack.
"""

import asyncio
import datetime
import struct
from enum import Enum
from typing import Callable, Optional

from bleak import BleakClient

from radiacode.bytes_buffer import BytesBuffer
from radiacode.decoders.databuf import decode_VS_DATA_BUF
from radiacode.decoders.spectrum import decode_RC_VS_SPECTRUM
from radiacode.types import COMMAND, VS, VSFR, _VSFR_FORMATS

SERVICE_UUID = 'e63215e5-7003-49d8-96b0-b024798fb901'
WRITE_UUID = 'e63215e6-7003-49d8-96b0-b024798fb901'  # host -> device (write)
NOTIFY_UUID = 'e63215e7-7003-49d8-96b0-b024798fb901'  # device -> host (notify)

# raw-frame logger: (direction, reqtype, seq, payload_bytes)
RawLogger = Callable[[str, int, int, bytes], None]


class BLEFrameError(Exception):
    pass


class RadiaCodeBLE:
    """Command-level client bound to an already-connected BleakClient."""

    def __init__(self, client: BleakClient, raw_logger: Optional[RawLogger] = None):
        self._client = client
        self._raw_logger = raw_logger
        self._seq = 0
        self._base_time: Optional[datetime.datetime] = None
        self._spectrum_format_version = 0
        # notification reassembler state (one command in flight)
        self._resp_size = 0
        self._resp_buf = b''
        self._resp_future: Optional[asyncio.Future] = None
        self._lock = asyncio.Lock()

    @property
    def base_time(self) -> Optional[datetime.datetime]:
        return self._base_time

    @property
    def spectrum_format_version(self) -> int:
        return self._spectrum_format_version

    # ------------------------------------------------------------------
    # transport: notification reassembler (port of handleNotification, §3.2)
    # ------------------------------------------------------------------
    def _on_notify(self, _characteristic, data: bytearray) -> None:
        chunk = bytes(data)
        if self._resp_size == 0:
            if len(chunk) < 4:
                return  # malformed first fragment; ignore
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

    async def start_notifications(self) -> None:
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)

    # ------------------------------------------------------------------
    # command layer (port of radiacode.execute, §4)
    # ------------------------------------------------------------------
    async def execute(self, reqtype, args: bytes = b'', timeout: float = 10.0) -> BytesBuffer:
        async with self._lock:
            req_seq_no = 0x80 + self._seq
            self._seq = (self._seq + 1) % 32
            req_header = struct.pack('<HBB', int(reqtype), 0, req_seq_no)
            request = req_header + (args or b'')
            full = struct.pack('<I', len(request)) + request

            if self._raw_logger:
                self._raw_logger('tx', int(reqtype), req_seq_no, full)

            # arm reassembler + response future BEFORE writing
            self._resp_size = 0
            self._resp_buf = b''
            loop = asyncio.get_running_loop()
            self._resp_future = loop.create_future()

            try:
                # §3.1: slice request into <=18-byte writes to the write characteristic
                for pos in range(0, len(full), 18):
                    await self._client.write_gatt_char(WRITE_UUID, full[pos:pos + 18], response=False)

                try:
                    payload = await asyncio.wait_for(self._resp_future, timeout=timeout)
                except asyncio.TimeoutError as ex:
                    raise BLEFrameError(
                        f'response timeout reqtype=0x{int(reqtype):04x} seq=0x{req_seq_no:02x}'
                    ) from ex
            finally:
                self._resp_future = None

            if self._raw_logger:
                self._raw_logger('rx', int(reqtype), req_seq_no, payload)

            br = BytesBuffer(payload)
            resp_header = br.unpack('<4s')[0]
            if resp_header != req_header:
                raise BLEFrameError(
                    f'echo header mismatch req={req_header.hex()} resp={resp_header.hex()}'
                )
            return br

    # ------------------------------------------------------------------
    # read helpers (port of radiacode.read_request / batch / data_buf / spectrum)
    # ------------------------------------------------------------------
    async def read_request(self, command_id) -> BytesBuffer:
        r = await self.execute(COMMAND.RD_VIRT_STRING, struct.pack('<I', int(command_id)))
        retcode, flen = r.unpack('<II')
        if retcode != 1:
            raise BLEFrameError(f'{command_id}: retcode {retcode}')
        # §8.1 firmware bug: stray trailing 0x00
        if r.size() == flen + 1 and r._data[-1] == 0x00:
            r._data = r._data[:-1]
        if r.size() != flen:
            raise BLEFrameError(f'{command_id}: size {r.size()} != flen {flen}')
        return r

    async def write_request(self, command_id, data: bytes = b'') -> None:
        r = await self.execute(COMMAND.WR_VIRT_SFR, struct.pack('<I', int(command_id)) + (data or b''))
        retcode = r.unpack('<I')[0]
        if retcode != 1:
            raise BLEFrameError(f'WR_VIRT_SFR {command_id}: retcode {retcode}')

    async def probe_vsfr(self, vsfr):
        """Single-VSFR batch read. Returns (ok: bool, value).

        Uses a 1-element batch so the validity bitmask is unambiguous (avoids the
        partial-failure ambiguity of multi-VSFR batches). value is decoded via
        _VSFR_FORMATS when available.
        """
        r = await self.execute(COMMAND.RD_VIRT_SFR_BATCH, struct.pack('<II', 1, int(vsfr)))
        valid = r.unpack('<I')[0]
        ok = bool(valid & 1)
        value = None
        if ok and r.size() >= 4:
            raw = r.unpack('<I')[0]
            fmt = _VSFR_FORMATS.get(vsfr)
            if fmt:
                vals = [x for x in struct.unpack('<' + fmt, struct.pack('<I', raw)) if x is not None]
                value = vals[0] if len(vals) == 1 else vals
            else:
                value = raw
        return ok, value

    async def data_buf(self):
        """Returns (raw_payload_bytes, decoded_records). raw is the VS data after
        retcode/flen, i.e. exactly what decode_VS_DATA_BUF consumes (kept for
        offline re-parsing / unknown-group detection)."""
        r = await self.read_request(VS.DATA_BUF)
        raw = r.data()
        records = decode_VS_DATA_BUF(BytesBuffer(raw), self._base_time)
        return raw, records

    async def spectrum(self):
        r = await self.read_request(VS.SPECTRUM)
        raw = r.data()
        spec = decode_RC_VS_SPECTRUM(BytesBuffer(raw), self._spectrum_format_version)
        return raw, spec

    # ------------------------------------------------------------------
    # identity / session
    # ------------------------------------------------------------------
    async def fw_version(self):
        r = await self.execute(COMMAND.GET_VERSION)
        boot_minor, boot_major = r.unpack('<HH')
        boot_date = r.unpack_string()
        target_minor, target_major = r.unpack('<HH')
        target_date = r.unpack_string()
        return (
            (boot_major, boot_minor, boot_date),
            (target_major, target_minor, target_date.strip('\x00')),
        )

    async def fw_signature(self) -> str:
        r = await self.execute(COMMAND.FW_SIGNATURE)
        signature = r.unpack('<I')[0]
        filename = r.unpack_string()
        idstring = r.unpack_string()
        return f'Signature: {signature:08X}, FileName="{filename}", IdString="{idstring}"'

    async def serial_number(self) -> str:
        r = await self.read_request(VS.SERIAL_NUMBER)
        return r.data().decode('ascii')

    async def configuration(self) -> str:
        r = await self.read_request(VS.CONFIGURATION)
        return r.data().decode('cp1251')

    async def commands(self) -> str:
        r = await self.read_request(VS.SFR_FILE)
        return r.data().decode('ascii')

    async def set_local_time(self, dt: datetime.datetime) -> None:
        d = struct.pack('<BBBBBBBB', dt.day, dt.month, dt.year - 2000, 0, dt.second, dt.minute, dt.hour, 0)
        await self.execute(COMMAND.SET_TIME, d)

    async def device_time(self, v: int) -> None:
        await self.write_request(VSFR.DEVICE_TIME, struct.pack('<I', v))

    async def init_session(self) -> str:
        """Full session init (§6). Returns the raw CONFIGURATION text."""
        await self.execute(COMMAND.SET_EXCHANGE, b'\x01\xff\x12\xff')
        await self.set_local_time(datetime.datetime.now())
        await self.device_time(0)
        self._base_time = datetime.datetime.now() + datetime.timedelta(seconds=128)
        cfg = await self.configuration()
        for line in cfg.split('\n'):
            if line.startswith('SpecFormatVersion'):
                try:
                    self._spectrum_format_version = int(line.split('=')[1])
                except (ValueError, IndexError):
                    pass
                break
        return cfg


def record_to_dict(r) -> dict:
    """Serialize a decoded DATA_BUF record (dataclass) to a JSON-friendly dict."""
    d = {'type': type(r).__name__}
    for k, v in vars(r).items():
        if isinstance(v, datetime.datetime):
            v = v.isoformat()
        elif isinstance(v, Enum):
            v = v.name
        d[k] = v
    return d
