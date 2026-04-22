"""Parse ext4 directory entries."""

from __future__ import annotations

import struct
from dataclasses import dataclass


# File type constants
FT_UNKNOWN = 0
FT_REG = 1
FT_DIR = 2
FT_SYMLINK = 7


@dataclass
class DirEntry:
    """A single ext4 directory entry."""
    inode: int          # 0 = deleted/unused slot
    name: str
    file_type: int      # 0 if INCOMPAT_FILETYPE is not set


def parse_directory_block(
    data: bytes, has_filetype: bool = True,
) -> list[DirEntry]:
    """Parse all directory entries in a directory data block."""
    entries: list[DirEntry] = []
    pos = 0
    limit = len(data)

    while pos + 8 <= limit:
        inode_num = struct.unpack_from('<I', data, pos)[0]
        rec_len = struct.unpack_from('<H', data, pos + 4)[0]

        if rec_len < 8 or pos + rec_len > limit:
            break

        if has_filetype:
            name_len = data[pos + 6]
            file_type = data[pos + 7]
        else:
            name_len = struct.unpack_from('<H', data, pos + 6)[0]
            file_type = 0

        if pos + 8 + name_len > limit:
            break

        name_bytes = bytes(data[pos + 8:pos + 8 + name_len])
        try:
            name = name_bytes.decode('utf-8')
        except UnicodeDecodeError:
            name = name_bytes.decode('utf-8', errors='replace')

        # Skip . and .. entries
        if inode_num != 0 and name not in ('.', '..'):
            entries.append(DirEntry(
                inode=inode_num,
                name=name,
                file_type=file_type,
            ))

        pos += rec_len

    return entries
