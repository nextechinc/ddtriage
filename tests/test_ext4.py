"""Tests for ext4 filesystem parser."""

import struct
import pytest

from ddtriage.ext4.superblock import (
    parse_superblock, ExtSuperblock, EXT4_MAGIC, EXT4_SUPERBLOCK_OFFSET,
    INCOMPAT_EXTENTS, INCOMPAT_64BIT, INCOMPAT_FILETYPE,
)
from ddtriage.ext4.group_desc import parse_group_descriptors, GroupDescriptor
from ddtriage.ext4.inode import (
    parse_inode, Inode, walk_extent_tree, Extent, inode_to_data_runs,
    FLAG_EXTENTS, S_IFDIR, S_IFREG, EXTENT_MAGIC,
)
from ddtriage.ext4.dir_entry import parse_directory_block, DirEntry, FT_DIR, FT_REG


def _build_superblock(
    block_size: int = 4096,
    inodes_per_group: int = 8192,
    blocks_per_group: int = 32768,
    inode_size: int = 256,
    total_blocks: int = 262144,
    total_inodes: int = 65536,
    features_incompat: int = INCOMPAT_EXTENTS | INCOMPAT_FILETYPE,
) -> bytes:
    """Build a minimal valid ext4 superblock (1024 bytes)."""
    sb = bytearray(1024)
    log_block_size = 0
    bs = block_size
    while bs > 1024:
        log_block_size += 1
        bs //= 2

    struct.pack_into('<I', sb, 0x00, total_inodes)
    struct.pack_into('<I', sb, 0x04, total_blocks)
    struct.pack_into('<I', sb, 0x14, 0 if block_size > 1024 else 1)  # first_data_block
    struct.pack_into('<I', sb, 0x18, log_block_size)
    struct.pack_into('<I', sb, 0x20, blocks_per_group)
    struct.pack_into('<I', sb, 0x28, inodes_per_group)
    struct.pack_into('<H', sb, 0x38, EXT4_MAGIC)
    struct.pack_into('<I', sb, 0x4C, 1)  # dynamic rev
    struct.pack_into('<I', sb, 0x54, 11)  # first_ino
    struct.pack_into('<H', sb, 0x58, inode_size)
    struct.pack_into('<I', sb, 0x60, features_incompat)
    struct.pack_into('<H', sb, 0x17C, 64 if features_incompat & INCOMPAT_64BIT else 32)
    return bytes(sb)


class TestSuperblock:
    def test_parse_basic(self):
        sb_data = _build_superblock()
        image = b'\x00' * EXT4_SUPERBLOCK_OFFSET + sb_data
        sb = parse_superblock(image)
        assert sb.magic == EXT4_MAGIC
        assert sb.block_size == 4096
        assert sb.inode_size == 256
        assert sb.has_extents is True
        assert sb.has_filetype is True

    def test_bad_magic(self):
        sb_data = bytearray(1024)
        struct.pack_into('<H', sb_data, 0x38, 0x1234)
        image = b'\x00' * EXT4_SUPERBLOCK_OFFSET + bytes(sb_data)
        with pytest.raises(ValueError, match="Not an ext"):
            parse_superblock(image)

    def test_gdt_entry_size_64bit(self):
        sb_data = _build_superblock(features_incompat=INCOMPAT_EXTENTS | INCOMPAT_64BIT)
        image = b'\x00' * EXT4_SUPERBLOCK_OFFSET + sb_data
        sb = parse_superblock(image)
        assert sb.gdt_entry_size == 64

    def test_gdt_entry_size_32bit(self):
        sb_data = _build_superblock(features_incompat=INCOMPAT_EXTENTS)
        image = b'\x00' * EXT4_SUPERBLOCK_OFFSET + sb_data
        sb = parse_superblock(image)
        assert sb.gdt_entry_size == 32

    def test_num_groups(self):
        # 262144 blocks / 32768 per group = 8 groups
        sb_data = _build_superblock(total_blocks=262144, blocks_per_group=32768)
        image = b'\x00' * EXT4_SUPERBLOCK_OFFSET + sb_data
        sb = parse_superblock(image)
        assert sb.num_groups == 8


class TestExtentTree:
    def _make_extent_header(self, entries: int, depth: int = 0, max_entries: int = 4) -> bytes:
        header = bytearray(12)
        struct.pack_into('<H', header, 0, EXTENT_MAGIC)
        struct.pack_into('<H', header, 2, entries)
        struct.pack_into('<H', header, 4, max_entries)
        struct.pack_into('<H', header, 6, depth)
        return bytes(header)

    def _make_leaf_extent(self, file_block: int, length: int, phys: int) -> bytes:
        entry = bytearray(12)
        struct.pack_into('<I', entry, 0, file_block)
        struct.pack_into('<H', entry, 4, length)
        struct.pack_into('<H', entry, 6, (phys >> 32) & 0xFFFF)
        struct.pack_into('<I', entry, 8, phys & 0xFFFFFFFF)
        return bytes(entry)

    def test_single_leaf_extent(self):
        node = self._make_extent_header(1) + self._make_leaf_extent(0, 10, 100)
        # Pad to 60 bytes (i_block size)
        node += b'\x00' * (60 - len(node))

        extents = walk_extent_tree(node, b'', 4096)
        assert len(extents) == 1
        assert extents[0].file_block == 0
        assert extents[0].length == 10
        assert extents[0].phys_block == 100
        assert extents[0].uninitialized is False

    def test_multiple_leaf_extents(self):
        node = self._make_extent_header(3) + \
               self._make_leaf_extent(0, 5, 100) + \
               self._make_leaf_extent(5, 3, 200) + \
               self._make_leaf_extent(8, 2, 150)

        extents = walk_extent_tree(node, b'', 4096)
        assert len(extents) == 3
        assert extents[0].phys_block == 100
        assert extents[1].phys_block == 200

    def test_uninitialized_extent(self):
        # length > 32768 marks uninitialized
        node = self._make_extent_header(1) + \
               self._make_leaf_extent(0, 32768 + 5, 100)

        extents = walk_extent_tree(node, b'', 4096)
        assert extents[0].uninitialized is True
        assert extents[0].length == 5  # real length

    def test_bad_magic(self):
        node = bytearray(12)
        # no magic
        extents = walk_extent_tree(bytes(node), b'', 4096)
        assert extents == []


class TestInode:
    def _build_inode(
        self, mode: int = S_IFREG | 0o644,
        size: int = 1000, flags: int = FLAG_EXTENTS,
        i_block: bytes = b'\x00' * 60,
    ) -> bytes:
        inode = bytearray(256)
        struct.pack_into('<H', inode, 0x00, mode)
        struct.pack_into('<I', inode, 0x04, size & 0xFFFFFFFF)
        struct.pack_into('<I', inode, 0x6C, size >> 32)
        struct.pack_into('<I', inode, 0x20, flags)
        struct.pack_into('<H', inode, 0x1A, 1)  # links_count
        inode[0x28:0x28 + 60] = i_block
        return bytes(inode)

    def test_parse_regular_file(self):
        data = self._build_inode()
        inode = parse_inode(data)
        assert inode.is_regular is True
        assert inode.is_directory is False
        assert inode.has_extents is True

    def test_parse_directory(self):
        data = self._build_inode(mode=S_IFDIR | 0o755)
        inode = parse_inode(data)
        assert inode.is_directory is True


class TestDirEntry:
    def _make_entry(self, inode: int, name: str, file_type: int, rec_len: int | None = None) -> bytes:
        name_bytes = name.encode('utf-8')
        name_len = len(name_bytes)
        base_size = 8 + name_len
        if rec_len is None:
            rec_len = (base_size + 3) & ~3  # round up to 4
        entry = bytearray(rec_len)
        struct.pack_into('<I', entry, 0, inode)
        struct.pack_into('<H', entry, 4, rec_len)
        entry[6] = name_len
        entry[7] = file_type
        entry[8:8 + name_len] = name_bytes
        return bytes(entry)

    def test_parse_single_entry(self):
        # . and .. entries are skipped
        dot = self._make_entry(2, '.', FT_DIR)
        dotdot = self._make_entry(2, '..', FT_DIR)
        file1 = self._make_entry(12, 'hello.txt', FT_REG)
        # Pad to make the last entry fill the block
        data = dot + dotdot + file1
        data = data + b'\x00' * (4096 - len(data))

        entries = parse_directory_block(data)
        # . and .. filtered out
        names = [e.name for e in entries]
        assert 'hello.txt' in names
        assert '.' not in names

    def test_deleted_entry_skipped(self):
        deleted = self._make_entry(0, 'deleted', FT_REG)  # inode 0
        real = self._make_entry(20, 'real.txt', FT_REG)
        data = deleted + real
        data = data + b'\x00' * (4096 - len(data))

        entries = parse_directory_block(data)
        names = [e.name for e in entries]
        assert 'real.txt' in names
        assert 'deleted' not in names


class TestGroupDescriptors:
    def test_parse_basic(self):
        sb_data = _build_superblock(block_size=4096)
        sb = parse_superblock(b'\x00' * EXT4_SUPERBLOCK_OFFSET + sb_data)

        # Build an image with GDT after superblock
        image = bytearray(b'\x00' * sb.gdt_start_block * sb.block_size)
        image.extend(b'\x00' * 4096)  # GDT block

        # Write one group descriptor (32 bytes)
        gdt_offset = sb.gdt_start_block * sb.block_size
        struct.pack_into('<I', image, gdt_offset + 0x08, 1000)  # inode_table at block 1000

        descriptors = parse_group_descriptors(bytes(image), sb)
        assert len(descriptors) >= 1
        assert descriptors[0].inode_table == 1000
