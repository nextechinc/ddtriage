"""Health indicators — cross-reference file data runs against mapfile coverage."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .mapfile.parser import Mapfile
from .mapfile.query import coverage_percentage, is_range_rescued
from .ntfs.data_runs import data_runs_to_byte_ranges
from .ntfs.tree import FileRecord


class HealthStatus(Enum):
    COMPLETE = "complete"       # 100% rescued or resident
    PARTIAL = "partial"         # some clusters rescued
    UNREAD = "unread"           # 0% rescued
    UNKNOWN = "unknown"         # no data runs (sparse / empty)


@dataclass
class FileHealth:
    """Health assessment for a single file."""
    status: HealthStatus
    coverage_pct: float         # 0.0 – 100.0
    bytes_in_image: int         # already rescued
    bytes_to_read: int          # not yet rescued
    total_bytes: int            # total data bytes on disk

    @property
    def indicator(self) -> str:
        """Color-coded block indicator for TUI display."""
        if self.status == HealthStatus.COMPLETE:
            return "██"
        elif self.status == HealthStatus.PARTIAL:
            return "▓▓"
        elif self.status == HealthStatus.UNREAD:
            return "░░"
        else:
            return "??"

    @property
    def style(self) -> str:
        """Textual/Rich style name for the indicator."""
        if self.status == HealthStatus.COMPLETE:
            return "green"
        elif self.status == HealthStatus.PARTIAL:
            return "yellow"
        elif self.status == HealthStatus.UNREAD:
            return "red"
        else:
            return "dim"


def compute_file_health(
    record: FileRecord,
    mapfile: Mapfile,
    cluster_size: int,
    partition_offset: int = 0,
) -> FileHealth:
    """Compute the health/coverage status of a file against the mapfile.

    For resident files (data stored in MFT), health is always COMPLETE.
    For directories or files with no data, health is UNKNOWN.
    """
    # Resident data — fully available in MFT
    if record.resident_data is not None:
        size = len(record.resident_data)
        return FileHealth(
            status=HealthStatus.COMPLETE,
            coverage_pct=100.0,
            bytes_in_image=size,
            bytes_to_read=0,
            total_bytes=size,
        )

    # No data runs — directory or empty/sparse file
    if not record.data_runs:
        return FileHealth(
            status=HealthStatus.UNKNOWN,
            coverage_pct=0.0,
            bytes_in_image=0,
            bytes_to_read=0,
            total_bytes=0,
        )

    # Non-resident: convert data runs to byte ranges and check mapfile
    byte_ranges = data_runs_to_byte_ranges(
        record.data_runs, cluster_size, partition_offset,
    )

    total = 0
    rescued = 0

    for offset, length in byte_ranges:
        if offset is None:
            # Sparse run — no disk read needed
            continue
        total += length
        pct = coverage_percentage(mapfile, offset, length)
        rescued += int(length * pct / 100.0)

    if total == 0:
        return FileHealth(
            status=HealthStatus.UNKNOWN,
            coverage_pct=0.0,
            bytes_in_image=0,
            bytes_to_read=0,
            total_bytes=0,
        )

    overall_pct = (rescued / total) * 100.0
    to_read = total - rescued

    if overall_pct >= 100.0:
        status = HealthStatus.COMPLETE
    elif overall_pct <= 0.0:
        status = HealthStatus.UNREAD
    else:
        status = HealthStatus.PARTIAL

    return FileHealth(
        status=status,
        coverage_pct=min(overall_pct, 100.0),
        bytes_in_image=rescued,
        bytes_to_read=to_read,
        total_bytes=total,
    )


def compute_tree_health(
    records: dict[int, FileRecord],
    mapfile: Mapfile,
    cluster_size: int,
    partition_offset: int = 0,
) -> dict[int, FileHealth]:
    """Compute health for all file records. Returns mft_index → FileHealth."""
    result: dict[int, FileHealth] = {}
    for mft_idx, record in records.items():
        result[mft_idx] = compute_file_health(
            record, mapfile, cluster_size, partition_offset,
        )
    return result
