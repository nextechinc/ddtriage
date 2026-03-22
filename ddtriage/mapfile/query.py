"""Query gddrescue mapfile coverage for byte ranges."""

from __future__ import annotations

from dataclasses import dataclass

from .parser import Mapfile, MapEntry, STATUS_FINISHED


@dataclass
class RangeStatus:
    """A sub-range with its recovery status."""
    pos: int
    size: int
    status: str

    @property
    def end(self) -> int:
        return self.pos + self.size


def is_range_rescued(mapfile: Mapfile, start: int, length: int) -> bool:
    """Return True if the entire byte range [start, start+length) is rescued (+)."""
    if length <= 0:
        return True
    for rs in get_range_status(mapfile, start, length):
        if rs.status != STATUS_FINISHED:
            return False
    return True


def get_range_status(mapfile: Mapfile, start: int, length: int) -> list[RangeStatus]:
    """Break a byte range into sub-ranges with their mapfile status.

    Regions not covered by any mapfile entry are reported as '?' (non-tried).
    """
    if length <= 0:
        return []

    end = start + length
    result: list[RangeStatus] = []
    cursor = start

    for entry in mapfile.entries:
        if entry.pos >= end:
            break
        if entry.end <= cursor:
            continue

        # Gap before this entry
        if entry.pos > cursor:
            gap_end = min(entry.pos, end)
            result.append(RangeStatus(cursor, gap_end - cursor, '?'))
            cursor = gap_end

        if cursor >= end:
            break

        # Overlap with this entry
        overlap_start = max(cursor, entry.pos)
        overlap_end = min(entry.end, end)
        if overlap_end > overlap_start:
            result.append(RangeStatus(overlap_start, overlap_end - overlap_start, entry.status))
            cursor = overlap_end

    # Trailing gap
    if cursor < end:
        result.append(RangeStatus(cursor, end - cursor, '?'))

    return result


def coverage_percentage(mapfile: Mapfile, start: int, length: int) -> float:
    """Return the percentage of [start, start+length) that is rescued (+)."""
    if length <= 0:
        return 100.0
    rescued = 0
    for rs in get_range_status(mapfile, start, length):
        if rs.status == STATUS_FINISHED:
            rescued += rs.size
    return (rescued / length) * 100.0
