"""Parse ext4 block group descriptors."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .superblock import ExtSuperblock


@dataclass
class GroupDescriptor:
    """One block group descriptor."""
    block_bitmap: int
    inode_bitmap: int
    inode_table: int            # block number of inode table
    free_blocks: int
    free_inodes: int
    used_dirs: int
    flags: int


def parse_group_descriptors(
    data, sb: ExtSuperblock, partition_offset: int = 0,
) -> list[GroupDescriptor]:
    """Parse the block group descriptor table."""
    gdt_start = partition_offset + sb.gdt_start_block * sb.block_size
    num_groups = sb.num_groups
    entry_size = sb.gdt_entry_size

    descriptors: list[GroupDescriptor] = []
    for i in range(num_groups):
        off = gdt_start + i * entry_size
        if off + entry_size > len(data):
            break

        entry = bytes(data[off:off + entry_size])

        block_bitmap_lo = struct.unpack_from('<I', entry, 0x00)[0]
        inode_bitmap_lo = struct.unpack_from('<I', entry, 0x04)[0]
        inode_table_lo = struct.unpack_from('<I', entry, 0x08)[0]
        free_blocks_lo = struct.unpack_from('<H', entry, 0x0C)[0]
        free_inodes_lo = struct.unpack_from('<H', entry, 0x0E)[0]
        used_dirs_lo = struct.unpack_from('<H', entry, 0x10)[0]
        flags = struct.unpack_from('<H', entry, 0x12)[0]

        if sb.has_64bit and entry_size >= 64:
            block_bitmap_hi = struct.unpack_from('<I', entry, 0x20)[0]
            inode_bitmap_hi = struct.unpack_from('<I', entry, 0x24)[0]
            inode_table_hi = struct.unpack_from('<I', entry, 0x28)[0]
            free_blocks_hi = struct.unpack_from('<H', entry, 0x2C)[0]
            free_inodes_hi = struct.unpack_from('<H', entry, 0x2E)[0]
            used_dirs_hi = struct.unpack_from('<H', entry, 0x30)[0]
        else:
            block_bitmap_hi = 0
            inode_bitmap_hi = 0
            inode_table_hi = 0
            free_blocks_hi = 0
            free_inodes_hi = 0
            used_dirs_hi = 0

        descriptors.append(GroupDescriptor(
            block_bitmap=block_bitmap_lo | (block_bitmap_hi << 32),
            inode_bitmap=inode_bitmap_lo | (inode_bitmap_hi << 32),
            inode_table=inode_table_lo | (inode_table_hi << 32),
            free_blocks=free_blocks_lo | (free_blocks_hi << 16),
            free_inodes=free_inodes_lo | (free_inodes_hi << 16),
            used_dirs=used_dirs_lo | (used_dirs_hi << 16),
            flags=flags,
        ))

    return descriptors
