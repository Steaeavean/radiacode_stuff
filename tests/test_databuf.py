import datetime
import struct

from radiacode.bytes_buffer import BytesBuffer
from radiacode.decoders.databuf import decode_VS_DATA_BUF
from radiacode.types import DoseCounter, Event, EventId, RealTimeData


def _record(seq: int, eid: int, gid: int, ts_offset: int, payload: bytes) -> bytes:
    return struct.pack('<BBBi', seq, eid, gid, ts_offset) + payload


def test_dose_counter_does_not_desync_following_event():
    """Regression: upstream treated gid 0/4 as 16-byte UserData; real layout is 6-byte DoseCounter."""
    base = datetime.datetime(2020, 1, 1, 12, 0, 0)
    payload = b''.join(
        [
            _record(
                0,
                0,
                0,
                0,
                struct.pack('<ffHHHB', 1.0, 2.0, 0, 0, 0, 0),
            ),
            _record(1, 0, 4, 10, struct.pack('<IH', 12345, 0x9000)),
            _record(2, 0, 7, 20, struct.pack('<BBH', int(EventId.POWER_ON), 0, 0)),
        ]
    )
    records = decode_VS_DATA_BUF(BytesBuffer(payload), base)
    assert len(records) == 3
    assert isinstance(records[0], RealTimeData)
    assert isinstance(records[1], DoseCounter)
    assert records[1].dose_counter == 12345
    assert records[1].flags == 0x9000
    assert isinstance(records[2], Event)
    assert records[2].event is EventId.POWER_ON
