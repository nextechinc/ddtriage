"""LZNT1 decompression — the compression algorithm used by NTFS.

NTFS compresses data in compression units (typically 16 clusters = 64KB).
Each compression unit is divided into 4096-byte chunks. Each chunk starts
with a 2-byte header:
  - Bits 0-11: size of the compressed chunk data (minus 3)
  - Bit 15: 1 = compressed, 0 = not compressed (stored literally)
  - Bits 12-14: signature (must be 0b011 = 3)

Within a compressed chunk, data is a stream of tokens:
  - A flag byte where each bit indicates whether the next item is a
    literal byte (0) or a back-reference (1), LSB first.
  - Literals are single bytes copied to output.
  - Back-references are 2 bytes encoding (offset, length) where the
    split point between offset and length fields depends on the current
    output position within the chunk.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Standard NTFS compression unit = 16 clusters * 4096 bytes = 65536 bytes
COMPRESSION_UNIT_SIZE = 65536


def decompress_lznt1(data: bytes) -> bytes:
    """Decompress LZNT1-compressed data.

    Args:
        data: Raw compressed bytes (one or more compression units).

    Returns decompressed bytes.
    """
    output = bytearray()
    pos = 0

    while pos + 2 <= len(data):
        # Read chunk header
        header = int.from_bytes(data[pos:pos + 2], 'little')
        pos += 2

        if header == 0:
            # End of compressed data
            break

        chunk_size = (header & 0x0FFF) + 3  # size includes the 2-byte header? No — size of data after header
        # Actually: chunk_size = (header & 0x0FFF) + 1 gives the number of bytes
        # of chunk data (not including the 2-byte header itself).
        # The +3 above is wrong. Let me recalculate.
        # Per MS docs: the chunk size field (bits 0-11) + 1 = size of the entire
        # chunk including the 2-byte header. So data bytes = (header & 0x0FFF) + 1 - 2.
        chunk_data_size = (header & 0x0FFF) + 1
        is_compressed = bool(header & 0x8000)
        signature = (header >> 12) & 0x07

        if signature != 3:
            log.debug("Bad LZNT1 chunk signature %d at offset %d", signature, pos - 2)
            break

        chunk_end = pos - 2 + chunk_data_size + 2
        # Correction: chunk header says total bytes including header = (header & 0xFFF) + 3
        # Let me use the standard interpretation:
        # Total chunk size (including 2-byte header) = (header & 0x0FFF) + 3
        chunk_total = (header & 0x0FFF) + 3
        chunk_data = data[pos:pos - 2 + chunk_total]
        chunk_data_len = len(chunk_data)

        if not is_compressed:
            # Uncompressed chunk — copy literally (up to 4096 bytes)
            output.extend(chunk_data[:4096])
        else:
            # Decompress this chunk
            _decompress_chunk(chunk_data, output)

        pos = pos - 2 + chunk_total

    return bytes(output)


def _decompress_chunk(chunk_data: bytes, output: bytearray) -> None:
    """Decompress a single LZNT1 compressed chunk, appending to output."""
    chunk_start = len(output)  # output position at start of this chunk
    pos = 0

    while pos < len(chunk_data):
        # Read flag byte
        if pos >= len(chunk_data):
            break
        flags = chunk_data[pos]
        pos += 1

        for bit in range(8):
            if pos >= len(chunk_data):
                break
            if len(output) - chunk_start >= 4096:
                # Chunk decompresses to at most 4096 bytes
                return

            if flags & (1 << bit):
                # Back-reference (2 bytes)
                if pos + 2 > len(chunk_data):
                    return
                token = int.from_bytes(chunk_data[pos:pos + 2], 'little')
                pos += 2

                # The offset/length split depends on current position in chunk
                cur_pos = len(output) - chunk_start
                length_bits = _length_bits(cur_pos)
                offset_bits = 16 - length_bits

                back_offset = (token >> length_bits) + 1
                match_length = (token & ((1 << length_bits) - 1)) + 3

                # Copy from back-reference (may overlap — byte at a time)
                src = len(output) - back_offset
                if src < 0:
                    # Invalid back-reference — fill with zeros
                    output.extend(b'\x00' * match_length)
                else:
                    for _ in range(match_length):
                        if src < len(output):
                            output.append(output[src])
                        else:
                            output.append(0)
                        src += 1
            else:
                # Literal byte
                output.append(chunk_data[pos])
                pos += 1


def _length_bits(position: int) -> int:
    """Calculate the number of bits used for the length field in a back-reference.

    This depends on the current decompressed position within the chunk.
    The number of offset bits needed = ceil(log2(position)), minimum 4.
    Length bits = 16 - offset_bits.
    """
    if position < 0x10:
        return 12   # offset_bits = 4
    elif position < 0x20:
        return 11   # offset_bits = 5
    elif position < 0x40:
        return 10
    elif position < 0x80:
        return 9
    elif position < 0x100:
        return 8
    elif position < 0x200:
        return 7
    elif position < 0x400:
        return 6
    elif position < 0x800:
        return 5
    else:
        return 4    # offset_bits = 12


def decompress_compression_unit(
    raw_data: bytes,
    expected_size: int = COMPRESSION_UNIT_SIZE,
) -> bytes:
    """Decompress a single NTFS compression unit.

    A compression unit is typically 16 clusters (64KB). If the raw data
    is shorter than expected_size, it's compressed. If it's exactly
    expected_size, it's stored uncompressed.

    Args:
        raw_data: The raw bytes of the compression unit.
        expected_size: Expected decompressed size (default 64KB).

    Returns decompressed data, padded to expected_size with zeros if needed.
    """
    if len(raw_data) == expected_size:
        # Not compressed — stored literally
        return raw_data

    if len(raw_data) == 0:
        # Sparse — all zeros
        return b'\x00' * expected_size

    result = decompress_lznt1(raw_data)

    # Pad if decompressed data is shorter than expected
    if len(result) < expected_size:
        result = result + b'\x00' * (expected_size - len(result))

    return result[:expected_size]
