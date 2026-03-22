"""Parse NTFS boot sector (BPB — BIOS Parameter Block)."""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class BootSector:
    """Parsed NTFS boot sector."""
    oem_id: str
    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_start_lcn: int
    mft_mirror_lcn: int
    clusters_per_mft_record: int   # raw value (may be negative)
    clusters_per_index_block: int  # raw value (may be negative)
    volume_serial: int

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def mft_record_size(self) -> int:
        """MFT record size in bytes.

        If clusters_per_mft_record is negative, size is 2^|value|.
        Otherwise it's clusters_per_mft_record * cluster_size.
        """
        v = self.clusters_per_mft_record
        if v < 0:
            return 1 << (-v)
        return v * self.cluster_size

    @property
    def index_block_size(self) -> int:
        v = self.clusters_per_index_block
        if v < 0:
            return 1 << (-v)
        return v * self.cluster_size

    def mft_offset(self, partition_offset: int = 0) -> int:
        """Absolute byte offset of the MFT within the image."""
        return self.mft_start_lcn * self.cluster_size + partition_offset


BITLOCKER_SIGNATURE = b'-FVE-FS-'


class BitLockerDetected(Exception):
    """Raised when a BitLocker-encrypted volume is detected instead of NTFS."""
    pass


def detect_bitlocker(data: bytes, offset: int = 0) -> bool:
    """Check if the boot sector belongs to a BitLocker-encrypted volume.

    BitLocker replaces the NTFS OEM ID at offset 0x03 with '-FVE-FS-'.
    """
    if len(data) - offset < 16:
        return False
    return data[offset + 0x03:offset + 0x0B] == BITLOCKER_SIGNATURE


def parse_boot_sector(data: bytes, offset: int = 0) -> BootSector:
    """Parse an NTFS boot sector from raw bytes.

    Args:
        data: Raw bytes containing the boot sector (at least 512 bytes from offset).
        offset: Byte offset within data where the boot sector starts.

    Raises:
        BitLockerDetected: If the volume is BitLocker-encrypted.
        ValueError: If the boot sector is not valid NTFS.
    """
    if len(data) - offset < 512:
        raise ValueError(f"Need at least 512 bytes, got {len(data) - offset}")

    bs = data[offset:offset + 512]

    # Check for BitLocker before NTFS validation
    if detect_bitlocker(data, offset):
        raise BitLockerDetected(
            "This volume is BitLocker-encrypted. Use --recovery-key or "
            "--bitlocker-password to decrypt it."
        )

    oem_id = bs[0x03:0x0B].decode('ascii', errors='replace').strip()
    if oem_id != 'NTFS':
        raise ValueError(f"Not an NTFS boot sector: OEM ID is {oem_id!r}")

    bytes_per_sector = struct.unpack_from('<H', bs, 0x0B)[0]
    if bytes_per_sector not in (512, 1024, 2048, 4096):
        raise ValueError(f"Invalid bytes per sector: {bytes_per_sector}")

    sectors_per_cluster = bs[0x0D]
    if sectors_per_cluster == 0 or (sectors_per_cluster & (sectors_per_cluster - 1)) != 0:
        raise ValueError(f"Sectors per cluster must be a power of 2, got {sectors_per_cluster}")

    total_sectors = struct.unpack_from('<Q', bs, 0x28)[0]
    mft_start_lcn = struct.unpack_from('<Q', bs, 0x30)[0]
    mft_mirror_lcn = struct.unpack_from('<Q', bs, 0x38)[0]

    # These are signed bytes: negative means 2^|value|
    clusters_per_mft_record = struct.unpack_from('<b', bs, 0x40)[0]
    clusters_per_index_block = struct.unpack_from('<b', bs, 0x44)[0]

    volume_serial = struct.unpack_from('<Q', bs, 0x48)[0]

    return BootSector(
        oem_id=oem_id,
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        total_sectors=total_sectors,
        mft_start_lcn=mft_start_lcn,
        mft_mirror_lcn=mft_mirror_lcn,
        clusters_per_mft_record=clusters_per_mft_record,
        clusters_per_index_block=clusters_per_index_block,
        volume_serial=volume_serial,
    )
