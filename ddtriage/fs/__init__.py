"""Filesystem abstraction layer.

All filesystem parsers produce the same FileRecord / DirectoryTree types
(defined in ddtriage.ntfs.tree for historical reasons, but they're generic).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..ntfs.tree import FileRecord, DirectoryTree


class FilesystemParser(ABC):
    """Abstract base class for filesystem parsers.

    Each filesystem implementation reads from a disk image and produces
    a DirectoryTree of FileRecord objects. The rest of the pipeline
    (TUI, health, recovery, extraction) is filesystem-agnostic.
    """

    @abstractmethod
    def parse(
        self,
        image_data,
        partition_offset: int,
        progress_callback=None,
    ) -> DirectoryTree:
        """Parse the filesystem and return a directory tree.

        Args:
            image_data: Raw image bytes or memory-mapped file.
            partition_offset: Byte offset of the partition in the image.
            progress_callback: Optional callable(index, record) per entry.
        """
        ...

    @abstractmethod
    def get_cluster_size(self) -> int:
        """Return the cluster/block size in bytes."""
        ...

    @abstractmethod
    def get_label(self) -> str:
        """Return the filesystem type label (e.g. 'NTFS', 'FAT32')."""
        ...


def detect_filesystem(data: bytes, offset: int = 0) -> str | None:
    """Detect filesystem type from boot sector / superblock bytes.

    Args:
        data: Raw bytes from the image (at least 4096 bytes from offset).
        offset: Byte offset where the partition starts.

    Returns filesystem type string ('ntfs', 'fat32', 'fat16', 'exfat', 'ext4')
    or None if unrecognized.
    """
    if len(data) - offset < 512:
        return None

    bs = data[offset:offset + 4096] if len(data) - offset >= 4096 else data[offset:]

    # NTFS: OEM ID "NTFS    " at offset 0x03
    oem = bs[0x03:0x0B]
    if oem == b'NTFS    ':
        return 'ntfs'

    # BitLocker: "-FVE-FS-" at offset 0x03
    if oem == b'-FVE-FS-':
        return 'bitlocker'

    # exFAT: OEM ID "EXFAT   " at offset 0x03
    if oem == b'EXFAT   ':
        return 'exfat'

    # FAT32/FAT16: check for FAT signature
    # FAT32 has "FAT32   " at offset 0x52
    if len(bs) > 0x5A and bs[0x52:0x5A] == b'FAT32   ':
        return 'fat32'
    # FAT16 has "FAT16   " at offset 0x36
    if len(bs) > 0x3E and bs[0x36:0x3E] == b'FAT16   ':
        return 'fat16'
    # FAT12
    if len(bs) > 0x3E and bs[0x36:0x3E] == b'FAT12   ':
        return 'fat12'

    # ext2/3/4: magic number 0xEF53 at offset 1080 (0x438) from partition start
    if len(bs) >= 0x43A:
        import struct
        ext_magic = struct.unpack_from('<H', bs, 0x438)[0]
        if ext_magic == 0xEF53:
            # Check for ext4 features
            if len(bs) >= 0x464:
                incompat = struct.unpack_from('<I', bs, 0x460)[0]
                if incompat & 0x40:  # INCOMPAT_EXTENTS
                    return 'ext4'
                if incompat & 0x04:  # INCOMPAT_JOURNAL (ext3)
                    return 'ext3'
            return 'ext2'

    return None


def get_parser(fs_type: str) -> FilesystemParser:
    """Get the appropriate filesystem parser for the given type.

    Raises ValueError if the filesystem type is not supported.
    """
    if fs_type in ('ntfs', 'bitlocker'):
        from ._ntfs import NTFSFilesystemParser
        return NTFSFilesystemParser()
    elif fs_type in ('fat32', 'fat16', 'fat12'):
        from ._fat import FATFilesystemParser
        return FATFilesystemParser()
    elif fs_type == 'exfat':
        from ._exfat import ExFATFilesystemParser
        return ExFATFilesystemParser()
    elif fs_type in ('ext4', 'ext3', 'ext2'):
        from ._ext4 import Ext4FilesystemParser
        return Ext4FilesystemParser()
    else:
        raise ValueError(f"Unsupported filesystem type: {fs_type!r}")
