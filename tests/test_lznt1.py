"""Tests for LZNT1 decompression."""

import struct
import pytest

from ddtriage.ntfs.lznt1 import (
    decompress_lznt1, decompress_compression_unit,
    _decompress_chunk, _length_bits,
    COMPRESSION_UNIT_SIZE,
)


def _make_uncompressed_chunk(data: bytes) -> bytes:
    """Create an uncompressed LZNT1 chunk (header + literal data)."""
    assert len(data) <= 4096
    # Chunk size = len(data) + 2 (header) - 3 stored in low 12 bits
    # Total chunk = (header & 0xFFF) + 3
    # So (header & 0xFFF) = len(data) + 2 - 3 = len(data) - 1
    size_field = len(data) - 1
    signature = 3  # bits 12-14
    compressed = 0  # bit 15 = 0 (uncompressed)
    header = size_field | (signature << 12) | (compressed << 15)
    return struct.pack('<H', header) + data


def _make_compressed_chunk(compressed_data: bytes) -> bytes:
    """Create a compressed LZNT1 chunk header."""
    size_field = len(compressed_data) - 1
    signature = 3
    compressed = 1  # bit 15 = 1
    header = size_field | (signature << 12) | (compressed << 15)
    return struct.pack('<H', header) + compressed_data


class TestLengthBits:
    def test_initial_position(self):
        assert _length_bits(0) == 12
        assert _length_bits(0x0F) == 12

    def test_mid_positions(self):
        assert _length_bits(0x10) == 11
        assert _length_bits(0x20) == 10
        assert _length_bits(0x40) == 9
        assert _length_bits(0x80) == 8
        assert _length_bits(0x100) == 7

    def test_large_position(self):
        assert _length_bits(0x800) == 4
        assert _length_bits(0xFFF) == 4


class TestDecompressLZNT1:
    def test_uncompressed_chunk(self):
        """An uncompressed chunk should be returned as-is."""
        data = b"Hello, World! " * 10  # 140 bytes
        chunk = _make_uncompressed_chunk(data)
        result = decompress_lznt1(chunk + b'\x00\x00')  # terminator
        assert result == data

    def test_empty_input(self):
        result = decompress_lznt1(b'\x00\x00')
        assert result == b''

    def test_compressed_literals_only(self):
        """A compressed chunk with only literal bytes (no back-references)."""
        # Build compressed data: flag byte 0x00 (all literals) + 8 literal bytes
        literals = b'\x00' + b'ABCDEFGH'
        chunk = _make_compressed_chunk(literals)
        result = decompress_lznt1(chunk + b'\x00\x00')
        assert result == b'ABCDEFGH'

    def test_compressed_with_backref(self):
        """A compressed chunk with a back-reference."""
        # First 8 literals: "ABCDABCD" but compressed as "ABCD" + backref
        # Flag byte: 0b00010000 = 0x10 (bit 4 set = 5th item is backref)
        # Items: A(lit), B(lit), C(lit), D(lit), backref(offset=4, len=4)
        #
        # At position 4: length_bits = 12, so offset_bits = 4
        # Back-reference: offset=4→ (4-1)=3 shifted left by 12, length=4→(4-3)=1
        # Token: (3 << 12) | 1 = 0x3001
        compressed = bytes([
            0x10,           # flag: bits 0-3 = literal, bit 4 = backref
            ord('A'), ord('B'), ord('C'), ord('D'),  # 4 literals
            0x01, 0x30,     # backref token 0x3001 (little-endian)
        ])
        chunk = _make_compressed_chunk(compressed)
        result = decompress_lznt1(chunk + b'\x00\x00')
        assert result == b'ABCDABCD'

    def test_multiple_uncompressed_chunks(self):
        """Multiple uncompressed chunks."""
        data1 = b"First chunk data."
        data2 = b"Second chunk data."
        chunks = _make_uncompressed_chunk(data1) + _make_uncompressed_chunk(data2) + b'\x00\x00'
        result = decompress_lznt1(chunks)
        assert result == data1 + data2


class TestDecompressCompressionUnit:
    def test_uncompressed_unit(self):
        """Full-size unit should pass through."""
        data = b'\xAA' * COMPRESSION_UNIT_SIZE
        result = decompress_compression_unit(data)
        assert result == data

    def test_empty_unit(self):
        """Empty unit = sparse = all zeros."""
        result = decompress_compression_unit(b'')
        assert result == b'\x00' * COMPRESSION_UNIT_SIZE
        assert len(result) == COMPRESSION_UNIT_SIZE

    def test_short_unit_padded(self):
        """Decompressed data shorter than expected gets zero-padded."""
        # Make a tiny uncompressed chunk
        data = b"tiny"
        chunk = _make_uncompressed_chunk(data) + b'\x00\x00'
        result = decompress_compression_unit(chunk, expected_size=4096)
        assert len(result) == 4096
        assert result[:4] == b"tiny"
        assert result[4:] == b'\x00' * 4092
