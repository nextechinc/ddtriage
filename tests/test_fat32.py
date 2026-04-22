"""Tests for FAT32 filesystem parser."""

import struct
import pytest

from ddtriage.fat32.boot_sector import parse_fat_boot_sector, FATBootSector
from ddtriage.fat32.fat_table import FATTable, FAT32_EOC
from ddtriage.fat32.dir_entry import parse_directory, FATDirEntry
from ddtriage.fs import detect_filesystem


def _build_fat32_boot_sector(
    bytes_per_sector: int = 512,
    sectors_per_cluster: int = 8,
    reserved_sectors: int = 32,
    num_fats: int = 2,
    total_sectors: int = 1048576,
    fat_size_32: int = 1024,
    root_cluster: int = 2,
) -> bytes:
    data = bytearray(512)
    data[0:3] = b'\xEB\x58\x90'
    data[0x03:0x0B] = b'MSDOS5.0'
    struct.pack_into('<H', data, 0x0B, bytes_per_sector)
    data[0x0D] = sectors_per_cluster
    struct.pack_into('<H', data, 0x0E, reserved_sectors)
    data[0x10] = num_fats
    struct.pack_into('<H', data, 0x11, 0)  # root_entry_count = 0 for FAT32
    struct.pack_into('<H', data, 0x13, 0)  # total_sectors_16 = 0
    data[0x15] = 0xF8  # media type
    struct.pack_into('<H', data, 0x16, 0)  # fat_size_16 = 0
    struct.pack_into('<I', data, 0x20, total_sectors)
    struct.pack_into('<I', data, 0x24, fat_size_32)
    struct.pack_into('<I', data, 0x2C, root_cluster)
    struct.pack_into('<H', data, 0x30, 1)  # fs_info_sector
    struct.pack_into('<H', data, 0x32, 6)  # backup boot sector
    data[0x52:0x5A] = b'FAT32   '
    return bytes(data)


class TestFATBootSector:
    def test_parse_fat32(self):
        data = _build_fat32_boot_sector()
        bs = parse_fat_boot_sector(data)
        assert bs.fs_type == "FAT32"
        assert bs.bytes_per_sector == 512
        assert bs.sectors_per_cluster == 8
        assert bs.cluster_size == 4096
        assert bs.reserved_sectors == 32
        assert bs.root_cluster == 2

    def test_fat_start(self):
        data = _build_fat32_boot_sector(reserved_sectors=32)
        bs = parse_fat_boot_sector(data)
        assert bs.fat_start == 32 * 512

    def test_data_start(self):
        data = _build_fat32_boot_sector(reserved_sectors=32, num_fats=2, fat_size_32=1024)
        bs = parse_fat_boot_sector(data)
        expected = (32 + 2 * 1024) * 512
        assert bs.data_start == expected

    def test_cluster_to_offset(self):
        data = _build_fat32_boot_sector()
        bs = parse_fat_boot_sector(data)
        # Cluster 2 should be at data_start
        assert bs.cluster_to_offset(2) == bs.data_start
        # Cluster 3 should be one cluster later
        assert bs.cluster_to_offset(3) == bs.data_start + bs.cluster_size

    def test_too_short(self):
        with pytest.raises(ValueError):
            parse_fat_boot_sector(b'\x00' * 100)


class TestFATTable:
    def _make_fat_and_image(self, entries: dict[int, int]):
        """Build a minimal image with a FAT containing the given entries."""
        bs_data = _build_fat32_boot_sector()
        bs = parse_fat_boot_sector(bs_data)

        # Build FAT: each entry is 4 bytes
        max_cluster = max(entries.keys()) if entries else 2
        fat_size = (max_cluster + 1) * 4
        fat = bytearray(fat_size)
        for cluster, value in entries.items():
            struct.pack_into('<I', fat, cluster * 4, value)

        # Build image: boot sector + padding to FAT start + FAT + data
        image = bytearray(bs_data)
        image.extend(b'\x00' * (bs.fat_start - len(image)))
        image.extend(fat)
        image.extend(b'\x00' * (bs.data_start - len(image)))
        image.extend(b'\x00' * bs.cluster_size * 10)  # some data clusters

        return bytes(image), bs

    def _lcn_base(self, bs):
        """Calculate the LCN base for FAT cluster-to-LCN conversion."""
        return bs.data_start // bs.cluster_size

    def test_single_cluster_chain(self):
        image, bs = self._make_fat_and_image({2: FAT32_EOC})
        fat = FATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(2)
        assert runs == [(base, 1)]

    def test_consecutive_chain(self):
        image, bs = self._make_fat_and_image({2: 3, 3: 4, 4: FAT32_EOC})
        fat = FATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(2)
        # Consecutive clusters merge into one run
        assert runs == [(base, 3)]

    def test_fragmented_chain(self):
        image, bs = self._make_fat_and_image({2: 3, 3: FAT32_EOC, 5: 6, 6: FAT32_EOC})
        fat = FATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(2)
        assert runs == [(base, 2)]
        runs = fat.chain_to_data_runs(5)
        assert runs == [(base + 3, 2)]

    def test_noncontiguous_chain(self):
        image, bs = self._make_fat_and_image({2: 3, 3: 10, 10: 11, 11: FAT32_EOC})
        fat = FATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(2)
        assert runs == [(base, 2), (base + 8, 2)]


class TestDirEntry:
    def _make_short_entry(
        self, name: bytes = b'TEST    TXT',
        attr: int = 0x20, cluster: int = 5, size: int = 1234,
    ) -> bytes:
        entry = bytearray(32)
        entry[0:11] = name
        entry[11] = attr
        struct.pack_into('<H', entry, 0x14, cluster >> 16)  # cluster high
        struct.pack_into('<H', entry, 0x1A, cluster & 0xFFFF)  # cluster low
        struct.pack_into('<I', entry, 0x1C, size)
        return bytes(entry)

    def test_parse_short_entry(self):
        data = self._make_short_entry() + b'\x00' * 32  # terminator
        entries = parse_directory(data)
        assert len(entries) == 1
        assert entries[0].short_name == "TEST.TXT"
        assert entries[0].start_cluster == 5
        assert entries[0].size == 1234
        assert entries[0].is_directory is False

    def test_parse_directory_entry(self):
        data = self._make_short_entry(
            name=b'DOCS       ', attr=0x10, cluster=10, size=0,
        ) + b'\x00' * 32
        entries = parse_directory(data)
        assert len(entries) == 1
        assert entries[0].short_name == "DOCS"
        assert entries[0].is_directory is True

    def test_deleted_entry(self):
        data = bytearray(self._make_short_entry())
        data[0] = 0xE5  # deleted marker
        data += b'\x00' * 32
        entries = parse_directory(bytes(data))
        assert len(entries) == 1
        assert entries[0].is_deleted is True

    def test_end_of_directory(self):
        data = b'\x00' * 64
        entries = parse_directory(data)
        assert len(entries) == 0

    def test_skip_dot_dotdot(self):
        dot = self._make_short_entry(name=b'.          ', attr=0x10)
        dotdot = self._make_short_entry(name=b'..         ', attr=0x10)
        real = self._make_short_entry(name=b'FILE    TXT')
        data = dot + dotdot + real + b'\x00' * 32
        entries = parse_directory(data)
        assert len(entries) == 1
        assert entries[0].short_name == "FILE.TXT"


class TestFSDetection:
    def test_detect_fat32(self):
        data = _build_fat32_boot_sector()
        assert detect_filesystem(data) == 'fat32'

    def test_detect_ntfs(self):
        data = bytearray(512)
        data[0x03:0x0B] = b'NTFS    '
        assert detect_filesystem(bytes(data)) == 'ntfs'

    def test_detect_exfat(self):
        data = bytearray(512)
        data[0x03:0x0B] = b'EXFAT   '
        assert detect_filesystem(bytes(data)) == 'exfat'

    def test_detect_ext4(self):
        data = bytearray(0x470)
        struct.pack_into('<H', data, 0x438, 0xEF53)  # ext magic
        struct.pack_into('<I', data, 0x460, 0x40)     # INCOMPAT_EXTENTS
        assert detect_filesystem(bytes(data)) == 'ext4'

    def test_detect_unknown(self):
        data = b'\x00' * 4096
        assert detect_filesystem(data) is None
