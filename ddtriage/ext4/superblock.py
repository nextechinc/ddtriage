"""Parse ext2/3/4 superblock."""

from __future__ import annotations

import struct
from dataclasses import dataclass

EXT4_SUPERBLOCK_OFFSET = 1024
EXT4_MAGIC = 0xEF53

# Feature flags (s_feature_incompat)
INCOMPAT_FILETYPE = 0x0002
INCOMPAT_RECOVER = 0x0004
INCOMPAT_JOURNAL_DEV = 0x0008
INCOMPAT_META_BG = 0x0010
INCOMPAT_EXTENTS = 0x0040
INCOMPAT_64BIT = 0x0080
INCOMPAT_INLINE_DATA = 0x8000


@dataclass
class ExtSuperblock:
    """Parsed ext2/3/4 superblock."""
    inodes_count: int
    blocks_count: int            # combined 64-bit
    first_data_block: int
    log_block_size: int
    blocks_per_group: int
    inodes_per_group: int
    magic: int
    rev_level: int
    first_ino: int
    inode_size: int
    feature_incompat: int
    feature_ro_compat: int
    volume_name: str
    desc_size: int               # group descriptor size (32 or 64)

    @property
    def block_size(self) -> int:
        return 1024 << self.log_block_size

    @property
    def num_groups(self) -> int:
        import math
        return math.ceil(self.blocks_count / self.blocks_per_group)

    @property
    def has_64bit(self) -> bool:
        return bool(self.feature_incompat & INCOMPAT_64BIT)

    @property
    def has_extents(self) -> bool:
        return bool(self.feature_incompat & INCOMPAT_EXTENTS)

    @property
    def has_filetype(self) -> bool:
        return bool(self.feature_incompat & INCOMPAT_FILETYPE)

    @property
    def gdt_entry_size(self) -> int:
        if self.has_64bit and self.desc_size >= 64:
            return self.desc_size
        return 32

    @property
    def gdt_start_block(self) -> int:
        return self.first_data_block + 1


def parse_superblock(data, partition_offset: int = 0) -> ExtSuperblock:
    """Parse an ext2/3/4 superblock located 1024 bytes into the partition."""
    sb_offset = partition_offset + EXT4_SUPERBLOCK_OFFSET
    if sb_offset + 1024 > len(data):
        raise ValueError(f"Image too small for superblock at offset {sb_offset}")

    sb = bytes(data[sb_offset:sb_offset + 1024])

    magic = struct.unpack_from('<H', sb, 0x38)[0]
    if magic != EXT4_MAGIC:
        raise ValueError(f"Not an ext filesystem: magic is 0x{magic:04X}")

    inodes_count = struct.unpack_from('<I', sb, 0x00)[0]
    blocks_count_lo = struct.unpack_from('<I', sb, 0x04)[0]
    first_data_block = struct.unpack_from('<I', sb, 0x14)[0]
    log_block_size = struct.unpack_from('<I', sb, 0x18)[0]
    blocks_per_group = struct.unpack_from('<I', sb, 0x20)[0]
    inodes_per_group = struct.unpack_from('<I', sb, 0x28)[0]
    rev_level = struct.unpack_from('<I', sb, 0x4C)[0]

    if rev_level >= 1:
        first_ino = struct.unpack_from('<I', sb, 0x54)[0]
        inode_size = struct.unpack_from('<H', sb, 0x58)[0]
        feature_incompat = struct.unpack_from('<I', sb, 0x60)[0]
        feature_ro_compat = struct.unpack_from('<I', sb, 0x64)[0]
    else:
        first_ino = 11
        inode_size = 128
        feature_incompat = 0
        feature_ro_compat = 0

    # Volume name at 0x78, 16 bytes
    volume_name = sb[0x78:0x88].rstrip(b'\x00').decode('utf-8', errors='replace')

    # 64-bit extensions
    if len(sb) > 0x154:
        blocks_count_hi = struct.unpack_from('<I', sb, 0x150)[0]
    else:
        blocks_count_hi = 0

    # Descriptor size (at 0x17C)
    desc_size = 32
    if len(sb) > 0x17E:
        ds = struct.unpack_from('<H', sb, 0x17C)[0]
        if ds >= 32:
            desc_size = ds

    blocks_count = blocks_count_lo | (blocks_count_hi << 32)

    return ExtSuperblock(
        inodes_count=inodes_count,
        blocks_count=blocks_count,
        first_data_block=first_data_block,
        log_block_size=log_block_size,
        blocks_per_group=blocks_per_group,
        inodes_per_group=inodes_per_group,
        magic=magic,
        rev_level=rev_level,
        first_ino=first_ino,
        inode_size=inode_size,
        feature_incompat=feature_incompat,
        feature_ro_compat=feature_ro_compat,
        volume_name=volume_name,
        desc_size=desc_size,
    )
