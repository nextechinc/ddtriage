"""Tests for NTFS boot sector parsing."""

import struct
import pytest

from ddtriage.ntfs.boot_sector import parse_boot_sector, BootSector


def _build_boot_sector(
    oem_id: bytes = b'NTFS    ',
    bytes_per_sector: int = 512,
    sectors_per_cluster: int = 8,
    total_sectors: int = 1048575,
    mft_start_lcn: int = 786432,
    mft_mirror_lcn: int = 2,
    clusters_per_mft_record: int = -10,  # 2^10 = 1024
    clusters_per_index_block: int = -12,  # 2^12 = 4096
    volume_serial: int = 0xDEADBEEF,
) -> bytes:
    """Construct a 512-byte NTFS boot sector with the given parameters."""
    data = bytearray(512)

    # Jump instruction
    data[0:3] = b'\xEB\x52\x90'

    # OEM ID
    data[0x03:0x0B] = oem_id

    # Bytes per sector
    struct.pack_into('<H', data, 0x0B, bytes_per_sector)

    # Sectors per cluster
    data[0x0D] = sectors_per_cluster

    # Total sectors
    struct.pack_into('<Q', data, 0x28, total_sectors)

    # MFT start LCN
    struct.pack_into('<Q', data, 0x30, mft_start_lcn)

    # MFT mirror LCN
    struct.pack_into('<Q', data, 0x38, mft_mirror_lcn)

    # Clusters per MFT record (signed byte)
    struct.pack_into('<b', data, 0x40, clusters_per_mft_record)

    # Clusters per index block (signed byte)
    struct.pack_into('<b', data, 0x44, clusters_per_index_block)

    # Volume serial number
    struct.pack_into('<Q', data, 0x48, volume_serial)

    return bytes(data)


class TestBootSector:
    def test_parse_standard_boot_sector(self):
        data = _build_boot_sector()
        bs = parse_boot_sector(data)

        assert bs.oem_id == 'NTFS'
        assert bs.bytes_per_sector == 512
        assert bs.sectors_per_cluster == 8
        assert bs.cluster_size == 4096
        assert bs.total_sectors == 1048575
        assert bs.mft_start_lcn == 786432
        assert bs.mft_mirror_lcn == 2
        assert bs.mft_record_size == 1024  # 2^10
        assert bs.index_block_size == 4096  # 2^12
        assert bs.volume_serial == 0xDEADBEEF

    def test_mft_offset_no_partition_offset(self):
        data = _build_boot_sector(mft_start_lcn=100)
        bs = parse_boot_sector(data)
        assert bs.mft_offset() == 100 * 4096

    def test_mft_offset_with_partition_offset(self):
        data = _build_boot_sector(mft_start_lcn=100)
        bs = parse_boot_sector(data)
        assert bs.mft_offset(partition_offset=1048576) == 100 * 4096 + 1048576

    def test_parse_at_offset(self):
        prefix = b'\x00' * 256
        data = prefix + _build_boot_sector()
        bs = parse_boot_sector(data, offset=256)
        assert bs.oem_id == 'NTFS'
        assert bs.bytes_per_sector == 512

    def test_positive_clusters_per_mft_record(self):
        data = _build_boot_sector(clusters_per_mft_record=2, sectors_per_cluster=8)
        bs = parse_boot_sector(data)
        assert bs.mft_record_size == 2 * 4096  # 2 clusters * 4096 bytes

    def test_different_sector_sizes(self):
        for sector_size in (512, 1024, 2048, 4096):
            data = _build_boot_sector(bytes_per_sector=sector_size)
            bs = parse_boot_sector(data)
            assert bs.bytes_per_sector == sector_size
            assert bs.cluster_size == sector_size * 8

    def test_invalid_oem_id(self):
        data = _build_boot_sector(oem_id=b'FAT32   ')
        with pytest.raises(ValueError, match="Not an NTFS boot sector"):
            parse_boot_sector(data)

    def test_invalid_bytes_per_sector(self):
        data = _build_boot_sector(bytes_per_sector=256)
        with pytest.raises(ValueError, match="Invalid bytes per sector"):
            parse_boot_sector(data)

    def test_invalid_sectors_per_cluster(self):
        data = _build_boot_sector(sectors_per_cluster=3)
        with pytest.raises(ValueError, match="power of 2"):
            parse_boot_sector(data)

    def test_too_short(self):
        with pytest.raises(ValueError, match="at least 512 bytes"):
            parse_boot_sector(b'\x00' * 100)
