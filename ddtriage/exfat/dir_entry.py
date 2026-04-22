"""Parse exFAT directory entries (File + Stream Extension + File Name sets)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime

# Entry type codes (with InUse bit set = 0x80)
ENTRY_EOD = 0x00             # end of directory
ENTRY_BITMAP = 0x81          # allocation bitmap
ENTRY_UPCASE = 0x82          # upcase table
ENTRY_VOLUME_LABEL = 0x83    # volume label
ENTRY_FILE = 0x85            # file/directory
ENTRY_STREAM_EXT = 0xC0      # stream extension
ENTRY_FILE_NAME = 0xC1       # filename

# File attributes
ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20


@dataclass
class ExFATFileEntry:
    """A parsed exFAT file entry set (File + Stream + Name entries combined)."""
    name: str
    attributes: int
    start_cluster: int
    size: int                   # DataLength from stream extension
    valid_data_length: int
    no_fat_chain: bool          # True = contiguous clusters
    created: datetime | None
    modified: datetime | None
    accessed: datetime | None
    is_deleted: bool

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)


def _decode_exfat_timestamp(ts: int) -> datetime | None:
    """Decode exFAT 32-bit timestamp to datetime."""
    if ts == 0:
        return None
    try:
        second = (ts & 0x1F) * 2
        minute = (ts >> 5) & 0x3F
        hour = (ts >> 11) & 0x1F
        day = (ts >> 16) & 0x1F
        month = (ts >> 21) & 0x0F
        year = ((ts >> 25) & 0x7F) + 1980
        return datetime(year, month, day, hour, minute, second)
    except (ValueError, OverflowError):
        return None


def parse_directory(
    data: bytes,
    offset: int = 0,
    length: int | None = None,
) -> list[ExFATFileEntry]:
    """Parse exFAT directory entries from raw bytes.

    Handles the File (0x85) + Stream Extension (0xC0) + File Name (0xC1)
    entry sets that describe each file/directory.
    """
    entries: list[ExFATFileEntry] = []
    end = offset + (length or (len(data) - offset))
    pos = offset

    while pos + 32 <= end:
        entry_type = data[pos]

        if entry_type == ENTRY_EOD:
            break

        # Skip non-file entries (bitmap, upcase, volume label, unused)
        if entry_type != ENTRY_FILE:
            # Deleted file entries have InUse bit cleared: 0x05
            if entry_type == 0x05:
                # Try to parse as deleted file
                file_entry = _parse_file_entry_set(data, pos, end, is_deleted=True)
                if file_entry:
                    entries.append(file_entry)
                    # Skip past the entry set
                    secondary_count = data[pos + 1]
                    pos += 32 * (1 + secondary_count)
                    continue
            pos += 32
            continue

        # Parse file entry set: File (0x85) + secondaries
        file_entry = _parse_file_entry_set(data, pos, end, is_deleted=False)
        if file_entry:
            entries.append(file_entry)

        secondary_count = data[pos + 1]
        pos += 32 * (1 + secondary_count)
        continue

    return entries


def _parse_file_entry_set(
    data: bytes, pos: int, end: int, is_deleted: bool,
) -> ExFATFileEntry | None:
    """Parse a complete file entry set starting at pos."""
    if pos + 32 > end:
        return None

    # File entry (0x85 or 0x05 if deleted)
    secondary_count = data[pos + 1]
    if secondary_count < 2:
        return None  # need at least Stream + 1 Name entry

    attributes = struct.unpack_from('<H', data, pos + 0x04)[0]
    created_ts = struct.unpack_from('<I', data, pos + 0x0A)[0]
    modified_ts = struct.unpack_from('<I', data, pos + 0x0E)[0]
    accessed_ts = struct.unpack_from('<I', data, pos + 0x12)[0]

    # Stream Extension entry (should follow immediately)
    stream_pos = pos + 32
    if stream_pos + 32 > end:
        return None

    stream_type = data[stream_pos]
    # For deleted entries, stream type would be 0x40 (InUse cleared)
    if stream_type != ENTRY_STREAM_EXT and stream_type != 0x40:
        return None

    flags = data[stream_pos + 1]
    no_fat_chain = bool(flags & 0x02)
    name_length = data[stream_pos + 3]
    start_cluster = struct.unpack_from('<I', data, stream_pos + 0x14)[0]
    data_length = struct.unpack_from('<Q', data, stream_pos + 0x18)[0]
    valid_data_length = struct.unpack_from('<Q', data, stream_pos + 0x08)[0]

    # File Name entries (0xC1 or 0x41 if deleted)
    # Secondary entries: index 0 = stream (already parsed), index 1+ = name entries
    name_chars: list[str] = []
    for i in range(2, 1 + secondary_count):
        name_pos = pos + 32 * i
        if name_pos + 32 > end:
            break
        name_type = data[name_pos]
        if name_type != ENTRY_FILE_NAME and name_type != 0x41:
            break

        # 15 UTF-16LE characters at offset 2
        raw = data[name_pos + 2:name_pos + 32]
        try:
            part = raw.decode('utf-16-le')
            name_chars.append(part.split('\x00')[0])
        except UnicodeDecodeError:
            name_chars.append(raw.decode('utf-16-le', errors='replace').split('\x00')[0])

    name = ''.join(name_chars)[:name_length]
    if not name:
        return None

    return ExFATFileEntry(
        name=name,
        attributes=attributes,
        start_cluster=start_cluster,
        size=data_length,
        valid_data_length=valid_data_length,
        no_fat_chain=no_fat_chain,
        created=_decode_exfat_timestamp(created_ts),
        modified=_decode_exfat_timestamp(modified_ts),
        accessed=_decode_exfat_timestamp(accessed_ts),
        is_deleted=is_deleted,
    )
