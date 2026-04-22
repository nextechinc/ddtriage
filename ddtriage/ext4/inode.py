"""Parse ext4 inodes, including extent trees."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

# Inode flags
FLAG_EXTENTS = 0x00080000
FLAG_INLINE_DATA = 0x10000000
FLAG_HUGE_FILE = 0x00040000
FLAG_INDEX = 0x00001000       # htree directory

# i_mode masks
S_IFMT = 0xF000
S_IFDIR = 0x4000
S_IFREG = 0x8000
S_IFLNK = 0xA000

# Extent tree magic
EXTENT_MAGIC = 0xF30A


@dataclass
class Inode:
    """Parsed ext4 inode."""
    mode: int
    size: int                   # file size in bytes (64-bit combined)
    flags: int
    i_block: bytes              # 60 bytes — extent tree root OR indirect block pointers
    created: datetime | None
    modified: datetime | None
    accessed: datetime | None
    dtime: int                  # deletion time (0 = not deleted)
    links_count: int

    @property
    def is_directory(self) -> bool:
        return (self.mode & S_IFMT) == S_IFDIR

    @property
    def is_regular(self) -> bool:
        return (self.mode & S_IFMT) == S_IFREG

    @property
    def is_symlink(self) -> bool:
        return (self.mode & S_IFMT) == S_IFLNK

    @property
    def is_deleted(self) -> bool:
        return self.dtime != 0 or self.links_count == 0

    @property
    def has_extents(self) -> bool:
        return bool(self.flags & FLAG_EXTENTS)

    @property
    def has_inline_data(self) -> bool:
        return bool(self.flags & FLAG_INLINE_DATA)


def _decode_timestamp(ts: int) -> datetime | None:
    if ts == 0:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def parse_inode(data) -> Inode:
    """Parse an inode from raw bytes (at least 128 bytes)."""
    if len(data) < 128:
        raise ValueError(f"Inode data too small: {len(data)} bytes")

    mode = struct.unpack_from('<H', data, 0x00)[0]
    size_lo = struct.unpack_from('<I', data, 0x04)[0]
    atime = struct.unpack_from('<I', data, 0x08)[0]
    ctime = struct.unpack_from('<I', data, 0x0C)[0]
    mtime = struct.unpack_from('<I', data, 0x10)[0]
    dtime = struct.unpack_from('<I', data, 0x14)[0]
    links_count = struct.unpack_from('<H', data, 0x1A)[0]
    flags = struct.unpack_from('<I', data, 0x20)[0]
    i_block = bytes(data[0x28:0x28 + 60])
    size_hi = struct.unpack_from('<I', data, 0x6C)[0]

    size = size_lo | (size_hi << 32)

    return Inode(
        mode=mode,
        size=size,
        flags=flags,
        i_block=i_block,
        created=_decode_timestamp(ctime),
        modified=_decode_timestamp(mtime),
        accessed=_decode_timestamp(atime),
        dtime=dtime,
        links_count=links_count,
    )


@dataclass
class Extent:
    """A single leaf extent from the extent tree."""
    file_block: int             # logical block within the file
    length: int                 # number of blocks (real, not uninit-encoded)
    phys_block: int             # physical block on disk
    uninitialized: bool


def walk_extent_tree(
    node_bytes: bytes, image_data, block_size: int, partition_offset: int = 0,
    depth_limit: int = 5,
) -> list[Extent]:
    """Walk an extent tree starting at the given node.

    Returns a flat list of leaf extents sorted by file_block.
    """
    extents: list[Extent] = []
    _walk_node(node_bytes, image_data, block_size, partition_offset, extents, depth_limit)
    extents.sort(key=lambda e: e.file_block)
    return extents


def _walk_node(
    node: bytes, image_data, block_size: int, partition_offset: int,
    out: list[Extent], remaining_depth: int,
):
    if len(node) < 12 or remaining_depth < 0:
        return

    magic = struct.unpack_from('<H', node, 0)[0]
    if magic != EXTENT_MAGIC:
        return

    entries = struct.unpack_from('<H', node, 2)[0]
    max_entries = struct.unpack_from('<H', node, 4)[0]
    depth = struct.unpack_from('<H', node, 6)[0]

    if entries > max_entries or entries > 340:  # sanity
        return

    for i in range(entries):
        entry_off = 12 + i * 12
        if entry_off + 12 > len(node):
            break

        if depth == 0:
            # Leaf: ext4_extent
            file_block = struct.unpack_from('<I', node, entry_off)[0]
            ee_len = struct.unpack_from('<H', node, entry_off + 4)[0]
            start_hi = struct.unpack_from('<H', node, entry_off + 6)[0]
            start_lo = struct.unpack_from('<I', node, entry_off + 8)[0]

            uninitialized = ee_len > 32768
            real_len = ee_len - 32768 if uninitialized else ee_len
            phys = start_lo | (start_hi << 32)

            out.append(Extent(
                file_block=file_block,
                length=real_len,
                phys_block=phys,
                uninitialized=uninitialized,
            ))
        else:
            # Internal: ext4_extent_idx
            leaf_lo = struct.unpack_from('<I', node, entry_off + 4)[0]
            leaf_hi = struct.unpack_from('<H', node, entry_off + 8)[0]
            child_block = leaf_lo | (leaf_hi << 32)

            child_offset = partition_offset + child_block * block_size
            if child_offset + block_size > len(image_data):
                continue
            child_data = bytes(image_data[child_offset:child_offset + block_size])
            _walk_node(child_data, image_data, block_size, partition_offset,
                       out, remaining_depth - 1)


def inode_to_data_runs(
    inode: Inode, image_data, block_size: int, partition_offset: int = 0,
) -> list[tuple[int | None, int]]:
    """Convert an inode's data location info to (LCN, count) data runs.

    The runs are in logical file order, with sparse holes represented as
    (None, count). The "LCN" here is a block number relative to the image —
    when multiplied by block_size (as cluster_size) and combined with
    partition_offset by the recovery pipeline, it gives the correct byte
    offset:  byte = LCN * block_size + partition_offset
    """
    if inode.has_inline_data:
        return []  # inline data handled separately

    if inode.has_extents:
        extents = walk_extent_tree(inode.i_block, image_data, block_size, partition_offset)
        runs: list[tuple[int | None, int]] = []
        cursor = 0
        for ext in extents:
            if ext.file_block > cursor:
                # Sparse hole
                runs.append((None, ext.file_block - cursor))
            runs.append((ext.phys_block, ext.length))
            cursor = ext.file_block + ext.length
        return runs

    # Legacy indirect block pointers (ext2/3 compatibility)
    # i_block has 15 x 32-bit entries: 12 direct + 1 indirect + 1 dind + 1 tind
    runs = _parse_indirect_blocks(inode.i_block, image_data, block_size, partition_offset)
    return runs


def _parse_indirect_blocks(
    i_block: bytes, image_data, block_size: int, partition_offset: int,
) -> list[tuple[int | None, int]]:
    """Parse 12 direct + 1/2/3-indirect block pointers."""
    runs: list[tuple[int, int]] = []
    pointers = struct.unpack('<15I', i_block)

    # Direct blocks (0-11)
    for ptr in pointers[:12]:
        if ptr != 0:
            runs.append((ptr, 1))

    ptrs_per_block = block_size // 4

    # Single indirect (12)
    if pointers[12] != 0:
        for p in _read_indirect_block(
            pointers[12], image_data, block_size, partition_offset, 1, ptrs_per_block,
        ):
            runs.append((p, 1))

    # Double indirect (13)
    if pointers[13] != 0:
        for p in _read_indirect_block(
            pointers[13], image_data, block_size, partition_offset, 2, ptrs_per_block,
        ):
            runs.append((p, 1))

    # Triple indirect (14)
    if pointers[14] != 0:
        for p in _read_indirect_block(
            pointers[14], image_data, block_size, partition_offset, 3, ptrs_per_block,
        ):
            runs.append((p, 1))

    # Consolidate consecutive single-block runs into larger runs
    merged: list[tuple[int | None, int]] = []
    for block, count in runs:
        if merged and merged[-1][0] is not None and merged[-1][0] + merged[-1][1] == block:
            merged[-1] = (merged[-1][0], merged[-1][1] + count)
        else:
            merged.append((block, count))

    return merged


def _read_indirect_block(
    block_num: int, image_data, block_size: int, partition_offset: int,
    depth: int, ptrs_per_block: int,
) -> list[int]:
    """Recursively read an indirect block."""
    offset = partition_offset + block_num * block_size
    if offset + block_size > len(image_data):
        return []

    block_data = bytes(image_data[offset:offset + block_size])
    pointers = struct.unpack(f'<{ptrs_per_block}I', block_data)

    result: list[int] = []
    for ptr in pointers:
        if ptr == 0:
            continue
        if depth == 1:
            result.append(ptr)
        else:
            result.extend(_read_indirect_block(
                ptr, image_data, block_size, partition_offset,
                depth - 1, ptrs_per_block,
            ))
    return result
