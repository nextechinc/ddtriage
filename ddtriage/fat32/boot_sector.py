"""Parse FAT32/FAT16/FAT12 boot sector (BPB)."""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class FATBootSector:
    """Parsed FAT boot sector."""
    oem_id: str
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    root_entry_count: int       # 0 for FAT32
    total_sectors_16: int       # 0 for FAT32
    media_type: int
    fat_size_16: int            # 0 for FAT32
    total_sectors_32: int
    # FAT32 specific
    fat_size_32: int
    root_cluster: int           # first cluster of root directory
    fs_info_sector: int
    backup_boot_sector: int
    volume_label: str
    fs_type: str                # "FAT32", "FAT16", or "FAT12"

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def total_sectors(self) -> int:
        return self.total_sectors_32 if self.total_sectors_16 == 0 else self.total_sectors_16

    @property
    def fat_size(self) -> int:
        """FAT size in sectors."""
        return self.fat_size_32 if self.fat_size_16 == 0 else self.fat_size_16

    @property
    def fat_start(self) -> int:
        """Byte offset of the first FAT (relative to partition start)."""
        return self.reserved_sectors * self.bytes_per_sector

    @property
    def data_start(self) -> int:
        """Byte offset of the data region (relative to partition start)."""
        root_dir_sectors = 0
        if self.root_entry_count > 0:
            # FAT16/12: root directory is fixed size
            root_dir_sectors = ((self.root_entry_count * 32) +
                               (self.bytes_per_sector - 1)) // self.bytes_per_sector
        return (self.reserved_sectors +
                self.num_fats * self.fat_size +
                root_dir_sectors) * self.bytes_per_sector

    def cluster_to_offset(self, cluster: int) -> int:
        """Convert cluster number to byte offset relative to partition start.

        Clusters are numbered starting from 2.
        """
        return self.data_start + (cluster - 2) * self.cluster_size


def parse_fat_boot_sector(data: bytes, offset: int = 0) -> FATBootSector:
    """Parse a FAT boot sector from raw bytes."""
    if len(data) - offset < 512:
        raise ValueError(f"Need at least 512 bytes, got {len(data) - offset}")

    bs = data[offset:offset + 512]

    oem_id = bs[0x03:0x0B].decode('ascii', errors='replace').strip()

    bytes_per_sector = struct.unpack_from('<H', bs, 0x0B)[0]
    if bytes_per_sector not in (512, 1024, 2048, 4096):
        raise ValueError(f"Invalid bytes per sector: {bytes_per_sector}")

    sectors_per_cluster = bs[0x0D]
    if sectors_per_cluster == 0 or (sectors_per_cluster & (sectors_per_cluster - 1)) != 0:
        raise ValueError(f"Sectors per cluster must be a power of 2, got {sectors_per_cluster}")

    reserved_sectors = struct.unpack_from('<H', bs, 0x0E)[0]
    num_fats = bs[0x10]
    root_entry_count = struct.unpack_from('<H', bs, 0x11)[0]
    total_sectors_16 = struct.unpack_from('<H', bs, 0x13)[0]
    media_type = bs[0x15]
    fat_size_16 = struct.unpack_from('<H', bs, 0x16)[0]
    total_sectors_32 = struct.unpack_from('<I', bs, 0x20)[0]

    # FAT32-specific fields (offset 0x24+)
    fat_size_32 = struct.unpack_from('<I', bs, 0x24)[0]
    root_cluster = struct.unpack_from('<I', bs, 0x2C)[0]
    fs_info_sector = struct.unpack_from('<H', bs, 0x30)[0]
    backup_boot_sector = struct.unpack_from('<H', bs, 0x32)[0]

    # Determine FAT type and read volume label
    if fat_size_16 == 0 and fat_size_32 > 0:
        # FAT32: volume label at 0x47, fs type at 0x52
        volume_label = bs[0x47:0x52].decode('ascii', errors='replace').strip()
        fs_type_raw = bs[0x52:0x5A].decode('ascii', errors='replace').strip()
        fs_type = "FAT32"
    else:
        # FAT16/12: volume label at 0x2B, fs type at 0x36
        volume_label = bs[0x2B:0x36].decode('ascii', errors='replace').strip()
        fs_type_raw = bs[0x36:0x3E].decode('ascii', errors='replace').strip()
        fs_type = fs_type_raw if fs_type_raw in ("FAT16", "FAT12") else "FAT16"

    return FATBootSector(
        oem_id=oem_id,
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        reserved_sectors=reserved_sectors,
        num_fats=num_fats,
        root_entry_count=root_entry_count,
        total_sectors_16=total_sectors_16,
        media_type=media_type,
        fat_size_16=fat_size_16,
        total_sectors_32=total_sectors_32,
        fat_size_32=fat_size_32,
        root_cluster=root_cluster,
        fs_info_sector=fs_info_sector,
        backup_boot_sector=backup_boot_sector,
        volume_label=volume_label,
        fs_type=fs_type,
    )
