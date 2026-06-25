import pytest

from radiacode.bytes_buffer import BytesBuffer


def test_unpack_advances_position():
    br = BytesBuffer(b'\x01\x00\x00\x00\x02\x00')
    assert br.unpack('<I') == (1,)
    assert br.unpack('<H') == (2,)
    assert br.size() == 0


def test_unpack_raises_on_short_buffer():
    br = BytesBuffer(b'\x01')
    with pytest.raises(ValueError, match='4 bytes required'):
        br.unpack('<I')


def test_unpack_string():
    br = BytesBuffer(b'\x03ABCextra')
    assert br.unpack_string() == 'ABC'
    assert br.data() == b'extra'
