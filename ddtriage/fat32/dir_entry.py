"""Parse FAT directory entries (both short 8.3 and long filename entries)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime


# Directory entry attributes
ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = 0x0F  # combination marking a LFN entry


@dataclass
class FATDirEntry:
    """A parsed FAT directory entry."""
    short_name: str             # 8.3 name
    long_name: str              # LFN (empty if none)
    attributes: int
    start_cluster: int
    size: int
    created: datetime | None
    modified: datetime | None
    accessed: datetime | None
    is_deleted: bool

    @property
    def name(self) -> str:
        """Best available name (LFN preferred, 8.3 fallback)."""
        return self.long_name or self.short_name

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)

    @property
    def is_volume_label(self) -> bool:
        return bool(self.attributes & ATTR_VOLUME_ID)


def _decode_fat_date(date_val: int, time_val: int) -> datetime | None:
    """Decode FAT date/time fields to datetime."""
    if date_val == 0:
        return None
    try:
        day = date_val & 0x1F
        month = (date_val >> 5) & 0x0F
        year = ((date_val >> 9) & 0x7F) + 1980
        second = (time_val & 0x1F) * 2
        minute = (time_val >> 5) & 0x3F
        hour = (time_val >> 11) & 0x1F
        return datetime(year, month, day, hour, minute, second)
    except (ValueError, OverflowError):
        return None


def _decode_short_name(raw: bytes) -> str:
    """Decode an 8.3 short filename from 11 bytes."""
    name_part = raw[0:8].decode('ascii', errors='replace').rstrip()
    ext_part = raw[8:11].decode('ascii', errors='replace').rstrip()
    if ext_part:
        return f"{name_part}.{ext_part}"
    return name_part


def parse_directory(
    data: bytes,
    offset: int = 0,
    length: int | None = None,
) -> list[FATDirEntry]:
    """Parse FAT directory entries from raw bytes.

    Handles both short (8.3) and long filename (LFN) entries.
    """
    entries: list[FATDirEntry] = []
    lfn_parts: list[str] = []
    end = offset + (length or (len(data) - offset))
    pos = offset

    while pos + 32 <= end:
        first_byte = data[pos]

        # End of directory
        if first_byte == 0x00:
            break

        # Deleted entry marker
        is_deleted = first_byte == 0xE5

        attr = data[pos + 11]

        # Long filename entry
        if attr == ATTR_LONG_NAME and not is_deleted:
            seq = data[pos] & 0x3F
            last = bool(data[pos] & 0x40)

            # Extract LFN characters (UTF-16LE at specific offsets)
            chars = bytearray()
            chars.extend(data[pos + 1:pos + 11])    # 5 chars
            chars.extend(data[pos + 14:pos + 26])    # 6 chars
            chars.extend(data[pos + 28:pos + 32])    # 2 chars

            try:
                part = chars.decode('utf-16-le').split('\x00')[0].split('\uffff')[0]
            except UnicodeDecodeError:
                part = chars.decode('utf-16-le', errors='replace').split('\x00')[0]

            if last:
                lfn_parts = [''] * seq
            if 1 <= seq <= len(lfn_parts):
                lfn_parts[seq - 1] = part

            pos += 32
            continue

        # Skip volume label entries
        if attr & ATTR_VOLUME_ID and not (attr & ATTR_DIRECTORY):
            lfn_parts = []
            pos += 32
            continue

        # Short (8.3) directory entry
        short_name = _decode_short_name(data[pos:pos + 11])

        # Skip . and .. entries
        if short_name in ('.', '..'):
            lfn_parts = []
            pos += 32
            continue

        # Assemble long name
        long_name = ''.join(lfn_parts) if lfn_parts else ''
        lfn_parts = []

        # Start cluster: high 2 bytes at 0x14, low 2 bytes at 0x1A
        cluster_hi = struct.unpack_from('<H', data, pos + 0x14)[0]
        cluster_lo = struct.unpack_from('<H', data, pos + 0x1A)[0]
        start_cluster = (cluster_hi << 16) | cluster_lo

        size = struct.unpack_from('<I', data, pos + 0x1C)[0]

        # Timestamps
        ctime = struct.unpack_from('<H', data, pos + 0x0E)[0]
        cdate = struct.unpack_from('<H', data, pos + 0x10)[0]
        mtime = struct.unpack_from('<H', data, pos + 0x16)[0]
        mdate = struct.unpack_from('<H', data, pos + 0x18)[0]
        adate = struct.unpack_from('<H', data, pos + 0x12)[0]

        entries.append(FATDirEntry(
            short_name=short_name,
            long_name=long_name,
            attributes=attr,
            start_cluster=start_cluster,
            size=size,
            created=_decode_fat_date(cdate, ctime),
            modified=_decode_fat_date(mdate, mtime),
            accessed=_decode_fat_date(adate, 0),
            is_deleted=is_deleted,
        ))

        pos += 32

    return entries
