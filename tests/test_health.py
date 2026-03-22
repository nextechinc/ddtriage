"""Tests for file health indicator computation."""

import pytest

from ddtriage.health import (
    compute_file_health, compute_tree_health,
    HealthStatus, FileHealth,
)
from ddtriage.mapfile.parser import parse_mapfile
from ddtriage.ntfs.tree import FileRecord, ROOT_MFT_INDEX


MAPFILE_TEXT = """\
# test
0x00000000  +
0x00000000  0x00100000  +
0x00100000  0x00100000  ?
"""

CLUSTER_SIZE = 4096
PARTITION_OFFSET = 0


def _make_record(
    mft_index: int = 16,
    data_runs: list[tuple[int | None, int]] | None = None,
    resident_data: bytes | None = None,
    size: int = 0,
    is_directory: bool = False,
) -> FileRecord:
    return FileRecord(
        mft_index=mft_index,
        name="test",
        parent_mft_index=ROOT_MFT_INDEX,
        is_directory=is_directory,
        is_deleted=False,
        size=size,
        data_runs=data_runs or [],
        resident_data=resident_data,
        created=None,
        modified=None,
    )


class TestFileHealth:
    def setup_method(self):
        self.mf = parse_mapfile(MAPFILE_TEXT)

    def test_resident_file_always_complete(self):
        rec = _make_record(resident_data=b"Hello!", size=6)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.COMPLETE
        assert h.coverage_pct == 100.0
        assert h.bytes_to_read == 0

    def test_no_data_runs_unknown(self):
        rec = _make_record(data_runs=[], size=0)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.UNKNOWN

    def test_directory_unknown(self):
        rec = _make_record(is_directory=True)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.UNKNOWN

    def test_fully_rescued_clusters(self):
        # 10 clusters starting at LCN 0 → byte range 0x0-0xA000
        # All within the rescued region [0, 0x100000)
        rec = _make_record(data_runs=[(0, 10)], size=40960)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.COMPLETE
        assert h.coverage_pct == 100.0
        assert h.bytes_to_read == 0

    def test_fully_unread_clusters(self):
        # Clusters in the non-tried region [0x100000, 0x200000)
        lcn = 0x100000 // CLUSTER_SIZE  # LCN 256
        rec = _make_record(data_runs=[(lcn, 5)], size=20480)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.UNREAD
        assert h.coverage_pct == 0.0
        assert h.bytes_to_read == 5 * CLUSTER_SIZE

    def test_partial_coverage(self):
        # 10 clusters at LCN 0 (rescued) + 10 clusters at LCN 256 (not rescued)
        lcn_unread = 0x100000 // CLUSTER_SIZE
        rec = _make_record(data_runs=[(0, 10), (lcn_unread, 10)], size=81920)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert h.status == HealthStatus.PARTIAL
        assert 45.0 < h.coverage_pct < 55.0  # roughly 50%

    def test_sparse_runs_ignored(self):
        # Sparse run + rescued run
        rec = _make_record(data_runs=[(None, 10), (0, 5)], size=61440)
        h = compute_file_health(rec, self.mf, CLUSTER_SIZE, PARTITION_OFFSET)
        # Only the non-sparse 5 clusters matter, and they're rescued
        assert h.status == HealthStatus.COMPLETE
        assert h.bytes_to_read == 0

    def test_indicator_strings(self):
        h_complete = FileHealth(HealthStatus.COMPLETE, 100.0, 100, 0, 100)
        h_partial = FileHealth(HealthStatus.PARTIAL, 50.0, 50, 50, 100)
        h_unread = FileHealth(HealthStatus.UNREAD, 0.0, 0, 100, 100)
        h_unknown = FileHealth(HealthStatus.UNKNOWN, 0.0, 0, 0, 0)

        assert h_complete.indicator == "██"
        assert h_partial.indicator == "▓▓"
        assert h_unread.indicator == "░░"
        assert h_unknown.indicator == "??"

        assert h_complete.style == "green"
        assert h_partial.style == "yellow"
        assert h_unread.style == "red"
        assert h_unknown.style == "dim"


class TestTreeHealth:
    def test_bulk_computation(self):
        mf = parse_mapfile(MAPFILE_TEXT)
        records = {
            16: _make_record(mft_index=16, resident_data=b"x", size=1),
            17: _make_record(mft_index=17, data_runs=[(0, 5)], size=20480),
        }
        result = compute_tree_health(records, mf, CLUSTER_SIZE, PARTITION_OFFSET)
        assert 16 in result
        assert 17 in result
        assert result[16].status == HealthStatus.COMPLETE
        assert result[17].status == HealthStatus.COMPLETE
