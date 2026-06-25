"""BLE notification payload reassembler (radiacode-ble-protocol.md §3.2)."""

from __future__ import annotations

import struct
from dataclasses import dataclass


class ReassemblerUnderflow(Exception):
    """Raised when notification chunks exceed the declared payload length."""


@dataclass
class BleReassembler:
    """Reassemble length-prefixed BLE notification payloads from GATT chunks."""

    resp_size: int = 0
    resp_buf: bytes = b''

    def reset(self) -> None:
        self.resp_size = 0
        self.resp_buf = b''

    def feed(self, chunk: bytes, *, armed: bool = True) -> bytes | None:
        """Consume one notification chunk.

        Args:
            chunk: Raw bytes from a single GATT notification.
            armed: When False, ignore stray first fragments (no active command).

        Returns:
            Complete reassembled payload when done, else None.

        Raises:
            ReassemblerUnderflow: Declared length shorter than received data.
        """
        if self.resp_size == 0:
            if not armed:
                return None
            if len(chunk) < 4:
                return None
            self.resp_size = 4 + struct.unpack('<i', chunk[:4])[0]
            self.resp_buf = chunk[4:]
        else:
            self.resp_buf += chunk

        self.resp_size -= len(chunk)
        if self.resp_size < 0:
            underflow = self.resp_size
            self.reset()
            raise ReassemblerUnderflow(underflow)

        if self.resp_size == 0:
            payload = self.resp_buf
            self.resp_buf = b''
            return payload

        return None
