"""Tests for exFAT filesystem parser."""

import struct
import math
import pytest

from ddtriage.exfat.boot_sector import parse_exfat_boot_sector, ExFATBootSector
from ddtriage.exfat.fat_table import ExFATTable, EXFAT_EOC
from ddtriage.exfat.dir_entry import parse_directory, ExFATFileEntry, ENTRY_FILE, ENTRY_STREAM_EXT, ENTRY_FILE_NAME


def _build_exfat_boot_sector(
    bytes_per_sector_shift: int = 9,   # 512
    sectors_per_cluster_shift: int = 3, # 8 sectors = 4096 bytes
    fat_offset: int = 24,
    fat_length: int = 256,
    cluster_heap_offset: int = 512,
    cluster_count: int = 100000,
    root_cluster: int = 4,
) -> bytes:
    data = bytearray(512)
    data[0:3] = b'\xEB\x76\x90'
    data[0x03:0x0B] = b'EXFAT   '
    # Reserved area 0x0B-0x3F must be zero (already is)
    struct.pack_into('<Q', data, 0x40, 0)       # partition offset
    struct.pack_into('<Q', data, 0x48, 1000000)  # volume length
    struct.pack_into('<I', data, 0x50, fat_offset)
    struct.pack_into('<I', data, 0x54, fat_length)
    struct.pack_into('<I', data, 0x58, cluster_heap_offset)
    struct.pack_into('<I', data, 0x5C, cluster_count)
    struct.pack_into('<I', data, 0x60, root_cluster)
    struct.pack_into('<I', data, 0x64, 0xDEAD)   # serial
    struct.pack_into('<H', data, 0x68, 0x0100)    # revision 1.0
    struct.pack_into('<H', data, 0x6A, 0)         # flags
    data[0x6C] = bytes_per_sector_shift
    data[0x6D] = sectors_per_cluster_shift
    data[0x6E] = 1  # num FATs
    struct.pack_into('<H', data, 0x1FE, 0xAA55)
    return bytes(data)


class TestExFATBootSector:
    def test_parse(self):
        data = _build_exfat_boot_sector()
        bs = parse_exfat_boot_sector(data)
        assert bs.bytes_per_sector == 512
        assert bs.sectors_per_cluster == 8
        assert bs.cluster_size == 4096
        assert bs.root_cluster == 4
        assert bs.fat_offset == 24
        assert bs.cluster_heap_offset == 512

    def test_cluster_to_offset(self):
        data = _build_exfat_boot_sector()
        bs = parse_exfat_boot_sector(data)
        # Cluster 2 starts at heap_byte_offset
        assert bs.cluster_to_offset(2) == bs.heap_byte_offset
        # Cluster 3 is one cluster later
        assert bs.cluster_to_offset(3) == bs.heap_byte_offset + bs.cluster_size

    def test_not_exfat(self):
        data = bytearray(512)
        data[0x03:0x0B] = b'NTFS    '
        with pytest.raises(ValueError, match="Not an exFAT"):
            parse_exfat_boot_sector(bytes(data))


class TestExFATTable:
    def _make_fat_and_image(self, entries: dict[int, int]):
        bs_data = _build_exfat_boot_sector()
        bs = parse_exfat_boot_sector(bs_data)

        max_cluster = max(entries.keys()) if entries else 2
        fat_size = (max_cluster + 1) * 4
        fat = bytearray(fat_size)
        for cluster, value in entries.items():
            struct.pack_into('<I', fat, cluster * 4, value)

        image = bytearray(bs_data)
        image.extend(b'\x00' * (bs.fat_byte_offset - len(image)))
        image.extend(fat)
        image.extend(b'\x00' * (bs.heap_byte_offset - len(image)))
        image.extend(b'\x00' * bs.cluster_size * 20)

        return bytes(image), bs

    def _lcn_base(self, bs):
        return bs.heap_byte_offset // bs.cluster_size

    def test_single_cluster(self):
        image, bs = self._make_fat_and_image({4: EXFAT_EOC})
        fat = ExFATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(4)
        assert runs == [(base + 2, 1)]

    def test_consecutive(self):
        image, bs = self._make_fat_and_image({4: 5, 5: 6, 6: EXFAT_EOC})
        fat = ExFATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.chain_to_data_runs(4)
        assert runs == [(base + 2, 3)]

    def test_contiguous_data_runs(self):
        image, bs = self._make_fat_and_image({})
        fat = ExFATTable(image, bs)
        base = self._lcn_base(bs)
        runs = fat.contiguous_data_runs(4, 10)
        assert runs == [(base + 2, 10)]


class TestExFATDirEntry:
    def _make_file_entry_set(
        self, name: str = "test.txt", size: int = 1234,
        cluster: int = 10, is_dir: bool = False,
        no_fat_chain: bool = False,
    ) -> bytes:
        """Build a minimal exFAT file entry set (File + Stream + Name entries)."""
        name_bytes = name.encode('utf-16-le')
        name_entry_count = math.ceil(len(name) / 15)
        secondary_count = 1 + name_entry_count  # stream + name entries

        result = bytearray()

        # File entry (0x85)
        file_entry = bytearray(32)
        file_entry[0] = ENTRY_FILE
        file_entry[1] = secondary_count
        attrs = 0x10 if is_dir else 0x20
        struct.pack_into('<H', file_entry, 0x04, attrs)
        result.extend(file_entry)

        # Stream extension entry (0xC0)
        stream = bytearray(32)
        stream[0] = ENTRY_STREAM_EXT
        flags = 0x01  # AllocationPossible
        if no_fat_chain:
            flags |= 0x02
        stream[1] = flags
        stream[3] = len(name)
        struct.pack_into('<I', stream, 0x14, cluster)
        struct.pack_into('<Q', stream, 0x18, size)
        struct.pack_into('<Q', stream, 0x08, size)  # valid data length
        result.extend(stream)

        # File name entries (0xC1)
        for i in range(name_entry_count):
            name_entry = bytearray(32)
            name_entry[0] = ENTRY_FILE_NAME
            start = i * 15
            end_idx = min(start + 15, len(name))
            chunk = name[start:end_idx].encode('utf-16-le')
            name_entry[2:2 + len(chunk)] = chunk
            result.extend(name_entry)

        return bytes(result)

    def test_parse_file(self):
        data = self._make_file_entry_set("hello.txt", size=500, cluster=10)
        data += b'\x00' * 32  # EOD
        entries = parse_directory(data)
        assert len(entries) == 1
        assert entries[0].name == "hello.txt"
        assert entries[0].size == 500
        assert entries[0].start_cluster == 10
        assert entries[0].is_directory is False

    def test_parse_directory(self):
        data = self._make_file_entry_set("Documents", is_dir=True, cluster=20)
        data += b'\x00' * 32
        entries = parse_directory(data)
        assert len(entries) == 1
        assert entries[0].is_directory is True

    def test_no_fat_chain_flag(self):
        data = self._make_file_entry_set("contig.bin", no_fat_chain=True, cluster=5)
        data += b'\x00' * 32
        entries = parse_directory(data)
        assert entries[0].no_fat_chain is True

    def test_long_filename(self):
        long_name = "this is a really long filename with spaces.docx"
        data = self._make_file_entry_set(long_name, size=9999, cluster=50)
        data += b'\x00' * 32
        entries = parse_directory(data)
        assert entries[0].name == long_name

    def test_eod(self):
        entries = parse_directory(b'\x00' * 64)
        assert len(entries) == 0
