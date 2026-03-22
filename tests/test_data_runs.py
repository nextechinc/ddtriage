"""Tests for NTFS data run decoding."""

import pytest

from ddtriage.ntfs.data_runs import decode_data_runs, data_runs_to_byte_ranges


def _encode_run(length_bytes: bytes, offset_bytes: bytes | None = None) -> bytes:
    """Encode a single data run."""
    length_size = len(length_bytes)
    offset_size = len(offset_bytes) if offset_bytes else 0
    header = length_size | (offset_size << 4)
    result = bytes([header]) + length_bytes
    if offset_bytes:
        result += offset_bytes
    return result


class TestDecodeDataRuns:
    def test_single_run(self):
        # 1 byte length (10 clusters), 2 byte offset (LCN 1000)
        data = _encode_run(b'\x0A', b'\xE8\x03') + b'\x00'
        runs = decode_data_runs(data)
        assert runs == [(1000, 10)]

    def test_two_runs_relative_offset(self):
        # First: 5 clusters at LCN 100
        run1 = _encode_run(b'\x05', b'\x64')
        # Second: 3 clusters at LCN 100+50=150
        run2 = _encode_run(b'\x03', b'\x32')
        data = run1 + run2 + b'\x00'
        runs = decode_data_runs(data)
        assert runs == [(100, 5), (150, 3)]

    def test_negative_offset(self):
        # First: 5 clusters at LCN 200
        run1 = _encode_run(b'\x05', b'\xC8\x00')  # +200
        # Second: 3 clusters at LCN 200-50=150 (offset -50 as signed)
        run2 = _encode_run(b'\x03', b'\xCE\xFF')  # -50 in signed int16
        data = run1 + run2 + b'\x00'
        runs = decode_data_runs(data)
        assert runs == [(200, 5), (150, 3)]

    def test_sparse_run(self):
        # Sparse: offset_size=0, just length
        run1 = _encode_run(b'\x05', b'\x64')   # 5 clusters at LCN 100
        run2 = _encode_run(b'\x0A')             # 10 sparse clusters
        run3 = _encode_run(b'\x03', b'\x14')    # 3 clusters at LCN 100+20=120
        data = run1 + run2 + run3 + b'\x00'
        runs = decode_data_runs(data)
        assert runs == [(100, 5), (None, 10), (120, 3)]

    def test_empty_data(self):
        runs = decode_data_runs(b'\x00')
        assert runs == []

    def test_large_lcn(self):
        # 3-byte length, 4-byte offset for large volumes
        length = (500).to_bytes(3, 'little')
        offset = (1000000).to_bytes(4, 'little', signed=True)
        data = _encode_run(length, offset) + b'\x00'
        runs = decode_data_runs(data)
        assert runs == [(1000000, 500)]

    def test_multiple_fragments(self):
        # Simulate a fragmented file with 4 runs
        runs_data = b''
        runs_data += _encode_run(b'\x10', b'\x64\x00')  # 16 clusters at LCN 100
        runs_data += _encode_run(b'\x08', b'\x96\x00')  # 8 clusters at LCN 100+150=250
        runs_data += _encode_run(b'\x04', b'\x38\xFF')  # 4 clusters at LCN 250-200=50
        runs_data += _encode_run(b'\x20', b'\xC8\x00')  # 32 clusters at LCN 50+200=250... wait
        runs_data += b'\x00'

        runs = decode_data_runs(runs_data)
        assert len(runs) == 4
        assert runs[0] == (100, 16)
        assert runs[1] == (250, 8)
        # -200 signed: 0xFF38 = -200
        assert runs[2] == (50, 4)
        assert runs[3] == (250, 32)

    def test_with_offset_in_buffer(self):
        prefix = b'\xAA\xBB\xCC'
        run_data = _encode_run(b'\x05', b'\x64') + b'\x00'
        data = prefix + run_data
        runs = decode_data_runs(data, offset=3)
        assert runs == [(100, 5)]


class TestDataRunsByteRanges:
    def test_basic_conversion(self):
        runs = [(100, 5), (200, 3)]
        ranges = data_runs_to_byte_ranges(runs, cluster_size=4096)
        assert ranges == [(100 * 4096, 5 * 4096), (200 * 4096, 3 * 4096)]

    def test_with_partition_offset(self):
        runs = [(100, 5)]
        ranges = data_runs_to_byte_ranges(runs, cluster_size=4096, partition_offset=1048576)
        assert ranges == [(100 * 4096 + 1048576, 5 * 4096)]

    def test_sparse_runs(self):
        runs = [(100, 5), (None, 10), (200, 3)]
        ranges = data_runs_to_byte_ranges(runs, cluster_size=4096)
        assert ranges[0] == (100 * 4096, 5 * 4096)
        assert ranges[1] == (None, 10 * 4096)
        assert ranges[2] == (200 * 4096, 3 * 4096)
