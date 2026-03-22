"""Parse gddrescue mapfile (log file) format."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# Status codes used by gddrescue
STATUS_NON_TRIED = '?'
STATUS_NON_TRIMMED = '*'
STATUS_NON_SCRAPED = '/'
STATUS_BAD = '-'
STATUS_FINISHED = '+'

VALID_STATUSES = {STATUS_NON_TRIED, STATUS_NON_TRIMMED, STATUS_NON_SCRAPED,
                  STATUS_BAD, STATUS_FINISHED}


@dataclass
class MapEntry:
    """A single range entry in a gddrescue mapfile."""
    pos: int        # absolute byte offset
    size: int       # length in bytes
    status: str     # one of VALID_STATUSES

    @property
    def end(self) -> int:
        return self.pos + self.size


@dataclass
class Mapfile:
    """Parsed gddrescue mapfile."""
    comments: list[str] = field(default_factory=list)
    current_pos: int = 0
    current_status: str = STATUS_NON_TRIED
    entries: list[MapEntry] = field(default_factory=list)

    def find_entry(self, offset: int) -> int:
        """Binary search for the entry containing `offset`. Returns index or -1."""
        lo, hi = 0, len(self.entries) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            e = self.entries[mid]
            if offset < e.pos:
                hi = mid - 1
            elif offset >= e.end:
                lo = mid + 1
            else:
                return mid
        return -1


def parse_mapfile(text: str) -> Mapfile:
    """Parse a gddrescue mapfile from its text content.

    The format is:
      - Lines starting with '#' are comments.
      - First non-comment line: current_pos  current_status
      - Subsequent non-comment lines: pos  size  status
    """
    mf = Mapfile()
    got_header = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            mf.comments.append(line)
            continue

        parts = stripped.split()
        if not got_header:
            # First data line: current_pos current_status
            if len(parts) < 2:
                raise ValueError(f"Invalid header line: {line!r}")
            mf.current_pos = _parse_int(parts[0])
            mf.current_status = parts[1]
            got_header = True
        else:
            # Range line: pos size status
            if len(parts) < 3:
                raise ValueError(f"Invalid entry line: {line!r}")
            status = parts[2]
            if status not in VALID_STATUSES:
                raise ValueError(f"Unknown status {status!r} in line: {line!r}")
            mf.entries.append(MapEntry(
                pos=_parse_int(parts[0]),
                size=_parse_int(parts[1]),
                status=status,
            ))

    return mf


def parse_mapfile_from_path(path: str) -> Mapfile:
    """Parse a gddrescue mapfile from a file path."""
    with open(path, 'r') as f:
        return parse_mapfile(f.read())


def _parse_int(s: str) -> int:
    """Parse an integer that may be in hex (0x...) or decimal."""
    s = s.strip()
    if s.startswith('0x') or s.startswith('0X'):
        return int(s, 16)
    return int(s)
