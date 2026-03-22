"""Decode NTFS data run encoding (LCN/VCN cluster mapping)."""

from __future__ import annotations


def decode_data_runs(data: bytes, offset: int = 0) -> list[tuple[int | None, int]]:
    """Decode NTFS data runs from raw bytes.

    Returns a list of (lcn, cluster_count) tuples.
    lcn is None for sparse runs (no physical clusters allocated).

    The data run encoding:
      - Header byte: low nibble = bytes for length, high nibble = bytes for offset
      - Length: unsigned integer (run length in clusters)
      - Offset: signed integer, relative to previous LCN (absolute for first run)
      - Header byte 0x00 terminates the list
    """
    runs: list[tuple[int | None, int]] = []
    current_lcn = 0
    pos = offset

    while pos < len(data):
        header = data[pos]
        if header == 0x00:
            break
        pos += 1

        length_size = header & 0x0F
        offset_size = (header >> 4) & 0x0F

        if length_size == 0:
            break

        if pos + length_size + offset_size > len(data):
            break

        # Read run length (unsigned)
        run_length = int.from_bytes(data[pos:pos + length_size], 'little', signed=False)
        pos += length_size

        if offset_size == 0:
            # Sparse run — no physical clusters
            runs.append((None, run_length))
        else:
            # Read run offset (signed, relative to previous LCN)
            run_offset = int.from_bytes(data[pos:pos + offset_size], 'little', signed=True)
            pos += offset_size
            current_lcn += run_offset
            runs.append((current_lcn, run_length))

    return runs


def data_runs_to_byte_ranges(
    runs: list[tuple[int | None, int]],
    cluster_size: int,
    partition_offset: int = 0,
) -> list[tuple[int | None, int]]:
    """Convert data runs to absolute byte ranges.

    Returns list of (byte_offset, byte_length) tuples.
    byte_offset is None for sparse runs.
    """
    result: list[tuple[int | None, int]] = []
    for lcn, count in runs:
        byte_length = count * cluster_size
        if lcn is None:
            result.append((None, byte_length))
        else:
            byte_offset = lcn * cluster_size + partition_offset
            result.append((byte_offset, byte_length))
    return result
