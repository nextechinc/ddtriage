"""Parse exFAT boot sector (Volume Boot Record)."""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class ExFATBootSector:
    """Parsed exFAT boot sector."""
    partition_offset: int       # media-relative sector offset
    volume_length: int          # total sectors
    fat_offset: int             # sector offset to first FAT
    fat_length: int             # FAT length in sectors
    cluster_heap_offset: int    # sector offset to cluster heap (data start)
    cluster_count: int          # total clusters
    root_cluster: int           # first cluster of root directory
    volume_serial: int
    fs_revision: int
    volume_flags: int
    bytes_per_sector_shift: int
    sectors_per_cluster_shift: int
    num_fats: int

    @property
    def bytes_per_sector(self) -> int:
        return 1 << self.bytes_per_sector_shift

    @property
    def sectors_per_cluster(self) -> int:
        return 1 << self.sectors_per_cluster_shift

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def fat_byte_offset(self) -> int:
        """FAT byte offset relative to partition start."""
        return self.fat_offset * self.bytes_per_sector

    @property
    def heap_byte_offset(self) -> int:
        """Cluster heap byte offset relative to partition start."""
        return self.cluster_heap_offset * self.bytes_per_sector

    def cluster_to_offset(self, cluster: int) -> int:
        """Convert cluster number to byte offset relative to partition start."""
        return self.heap_byte_offset + (cluster - 2) * self.cluster_size


def parse_exfat_boot_sector(data: bytes, offset: int = 0) -> ExFATBootSector:
    """Parse an exFAT boot sector from raw bytes."""
    if len(data) - offset < 512:
        raise ValueError(f"Need at least 512 bytes, got {len(data) - offset}")

    bs = data[offset:offset + 512]

    fs_name = bs[0x03:0x0B]
    if fs_name != b'EXFAT   ':
        raise ValueError(f"Not an exFAT boot sector: filesystem name is {fs_name!r}")

    partition_offset = struct.unpack_from('<Q', bs, 0x40)[0]
    volume_length = struct.unpack_from('<Q', bs, 0x48)[0]
    fat_offset = struct.unpack_from('<I', bs, 0x50)[0]
    fat_length = struct.unpack_from('<I', bs, 0x54)[0]
    cluster_heap_offset = struct.unpack_from('<I', bs, 0x58)[0]
    cluster_count = struct.unpack_from('<I', bs, 0x5C)[0]
    root_cluster = struct.unpack_from('<I', bs, 0x60)[0]
    volume_serial = struct.unpack_from('<I', bs, 0x64)[0]
    fs_revision = struct.unpack_from('<H', bs, 0x68)[0]
    volume_flags = struct.unpack_from('<H', bs, 0x6A)[0]
    bytes_per_sector_shift = bs[0x6C]
    sectors_per_cluster_shift = bs[0x6D]
    num_fats = bs[0x6E]

    if bytes_per_sector_shift < 9 or bytes_per_sector_shift > 12:
        raise ValueError(f"Invalid bytes per sector shift: {bytes_per_sector_shift}")

    return ExFATBootSector(
        partition_offset=partition_offset,
        volume_length=volume_length,
        fat_offset=fat_offset,
        fat_length=fat_length,
        cluster_heap_offset=cluster_heap_offset,
        cluster_count=cluster_count,
        root_cluster=root_cluster,
        volume_serial=volume_serial,
        fs_revision=fs_revision,
        volume_flags=volume_flags,
        bytes_per_sector_shift=bytes_per_sector_shift,
        sectors_per_cluster_shift=sectors_per_cluster_shift,
        num_fats=num_fats,
    )
