import struct

import pytest

from radiacode.transports.reassembler import BleReassembler, ReassemblerUnderflow


def _chunk(payload: bytes) -> bytes:
    return struct.pack('<i', len(payload)) + payload


def test_single_chunk_complete():
    r = BleReassembler()
    assert r.feed(_chunk(b'hello')) == b'hello'


def test_multi_chunk():
    r = BleReassembler()
    payload = b'abcdefgh'
    first = struct.pack('<i', len(payload)) + payload[:2]
    assert r.feed(first) is None
    assert r.feed(payload[2:]) == payload


def test_short_first_fragment_ignored_when_armed():
    r = BleReassembler()
    assert r.feed(b'\x01\x02', armed=True) is None
    assert r.resp_size == 0


def test_stray_notification_when_disarmed():
    r = BleReassembler()
    assert r.feed(_chunk(b'ignored'), armed=False) is None
    assert r.resp_size == 0


def test_underflow_raises():
    r = BleReassembler()
    # Declared payload length 2 but chunk carries 6 payload bytes
    chunk = struct.pack('<i', 2) + b'abcdef'
    with pytest.raises(ReassemblerUnderflow):
        r.feed(chunk, armed=True)


def test_reset_clears_state():
    r = BleReassembler()
    r.feed(struct.pack('<i', 8) + b'ab', armed=True)
    r.reset()
    assert r.resp_size == 0
    assert r.resp_buf == b''
