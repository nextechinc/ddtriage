"""Tests for recovery orchestrator."""

import os
import tempfile
import pytest
from pathlib import Path

from ddtriage.mapfile.parser import parse_mapfile
from ddtriage.ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX
from ddtriage.recovery.orchestrator import (
    collect_byte_ranges, plan_recovery, assess_results, FileRecoveryStatus,
)


MAPFILE_TEXT = """\
# test
0x00000000  +
0x00000000  0x00100000  +
0x00100000  0x00100000  ?
"""

CLUSTER_SIZE = 4096
PARTITION_OFFSET = 0


def _make_record(
    mft_index: int, name: str, parent: int = ROOT_MFT_INDEX,
    data_runs: list | None = None, resident_data: bytes | None = None,
    size: int = 0, is_directory: bool = False,
) -> FileRecord:
    return FileRecord(
        mft_index=mft_index, name=name, parent_mft_index=parent,
        is_directory=is_directory, is_deleted=False, size=size,
        data_runs=data_runs or [], resident_data=resident_data,
        created=None, modified=None,
    )


def _build_tree(records: list[FileRecord]) -> DirectoryTree:
    root = _make_record(ROOT_MFT_INDEX, ".", is_directory=True)
    all_records = {ROOT_MFT_INDEX: root}
    for r in records:
        all_records[r.mft_index] = r
        root.children.append(r)
        r._parent = root  # type: ignore[attr-defined]
    root._parent = None  # type: ignore[attr-defined]
    return DirectoryTree(
        root=root, orphans=[], all_records=all_records,
        total_files=sum(1 for r in records if not r.is_directory),
        total_dirs=1,
    )


class TestCollectByteRanges:
    def test_basic(self):
        records = [
            _make_record(16, "a.bin", data_runs=[(0, 5)], size=20480),
            _make_record(17, "b.bin", data_runs=[(10, 3)], size=12288),
        ]
        ranges = collect_byte_ranges(records, CLUSTER_SIZE, PARTITION_OFFSET)
        # LCN 0 → 5 clusters, LCN 10 → 3 clusters; no overlap
        assert (0, 5 * 4096) in ranges
        assert (10 * 4096, 3 * 4096) in ranges

    def test_skips_resident(self):
        records = [
            _make_record(16, "tiny.txt", resident_data=b"hi", size=2),
        ]
        ranges = collect_byte_ranges(records, CLUSTER_SIZE, PARTITION_OFFSET)
        assert ranges == []

    def test_skips_sparse(self):
        records = [
            _make_record(16, "sparse.bin", data_runs=[(None, 10)], size=40960),
        ]
        ranges = collect_byte_ranges(records, CLUSTER_SIZE, PARTITION_OFFSET)
        assert ranges == []

    def test_merges_overlapping(self):
        records = [
            _make_record(16, "a.bin", data_runs=[(0, 10)], size=40960),
            _make_record(17, "b.bin", data_runs=[(5, 10)], size=40960),
        ]
        ranges = collect_byte_ranges(records, CLUSTER_SIZE, PARTITION_OFFSET)
        # Should merge: LCN 0-10 and LCN 5-15 → LCN 0-15
        assert len(ranges) == 1
        assert ranges[0] == (0, 15 * 4096)

    def test_with_partition_offset(self):
        records = [
            _make_record(16, "a.bin", data_runs=[(100, 5)], size=20480),
        ]
        ranges = collect_byte_ranges(records, CLUSTER_SIZE, partition_offset=1048576)
        assert ranges[0] == (100 * 4096 + 1048576, 5 * 4096)


class TestPlanRecovery:
    def test_plan_stats(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        f1 = _make_record(16, "rescued.bin", data_runs=[(0, 5)], size=20480)
        f2 = _make_record(17, "unread.bin",
                          data_runs=[(0x100000 // 4096, 5)], size=20480)
        tree = _build_tree([f1, f2])

        with tempfile.TemporaryDirectory() as td:
            plan = plan_recovery(
                {16, 17}, tree, mf, CLUSTER_SIZE, PARTITION_OFFSET, Path(td),
            )
            assert plan.file_count == 2
            assert plan.bytes_already_rescued > 0
            assert plan.bytes_to_read > 0


class TestAssessResults:
    def test_resident_complete(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        f1 = _make_record(16, "tiny.txt", resident_data=b"hi", size=2)
        tree = _build_tree([f1])

        results = assess_results({16}, tree, mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert len(results) == 1
        assert results[0].complete is True
        assert results[0].coverage_pct == 100.0

    def test_fully_rescued(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        f1 = _make_record(16, "ok.bin", data_runs=[(0, 5)], size=20480)
        tree = _build_tree([f1])

        results = assess_results({16}, tree, mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert results[0].complete is True

    def test_not_rescued(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        lcn = 0x100000 // CLUSTER_SIZE
        f1 = _make_record(16, "bad.bin", data_runs=[(lcn, 5)], size=20480)
        tree = _build_tree([f1])

        results = assess_results({16}, tree, mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert results[0].complete is False
        assert results[0].coverage_pct == 0.0

    def test_skips_directories(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        d = _make_record(16, "docs", is_directory=True)
        tree = _build_tree([d])

        results = assess_results({16}, tree, mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert len(results) == 0
