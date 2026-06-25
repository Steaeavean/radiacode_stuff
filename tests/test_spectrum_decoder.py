import datetime
import struct

import pytest

from radiacode.bytes_buffer import BytesBuffer
from radiacode.decoders.spectrum import decode_RC_VS_SPECTRUM, decode_counts_v0, decode_counts_v1


def test_decode_counts_v0():
    br = BytesBuffer(struct.pack('<III', 10, 20, 30))
    assert decode_counts_v0(br) == [10, 20, 30]


def test_decode_counts_v1_vlen_zero():
    # cnt=2, vlen=0 -> two zeros
    br = BytesBuffer(struct.pack('<H', (2 << 4) | 0))
    assert decode_counts_v1(br) == [0, 0]


def test_decode_counts_v1_delta():
    br = BytesBuffer(struct.pack('<HB', (1 << 4) | 1, 5))
    assert decode_counts_v1(br) == [5]
    br = BytesBuffer(struct.pack('<Hb', (1 << 4) | 2, 3))
    assert decode_counts_v1(br) == [3]


def test_decode_RC_VS_SPECTRUM_v0():
    body = struct.pack('<Ifff', 60, 1.0, 2.0, 3.0) + struct.pack('<II', 100, 200)
    spec = decode_RC_VS_SPECTRUM(BytesBuffer(body), 0)
    assert spec.duration == datetime.timedelta(seconds=60)
    assert spec.counts == [100, 200]


def test_decode_RC_VS_SPECTRUM_bad_version():
    body = struct.pack('<Ifff', 1, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match='unsupported format_version=2'):
        decode_RC_VS_SPECTRUM(BytesBuffer(body), 2)
