"""FAT32/FAT16/FAT12 filesystem parser adapter."""

from __future__ import annotations

from ..fat32.boot_sector import parse_fat_boot_sector
from ..fat32.fat_table import FATTable
from ..fat32.tree import build_fat_tree
from ..ntfs.tree import DirectoryTree
from . import FilesystemParser


class FATFilesystemParser(FilesystemParser):
    """Parser for FAT32, FAT16, and FAT12 filesystems."""

    def __init__(self):
        self._cluster_size = 4096
        self._fs_type = "FAT32"

    def parse(
        self,
        image_data,
        partition_offset: int,
        progress_callback=None,
        include_deleted: bool = False,
        **kwargs,
    ) -> DirectoryTree:
        bs = parse_fat_boot_sector(image_data, partition_offset)
        self._cluster_size = bs.cluster_size
        self._fs_type = bs.fs_type

        fat = FATTable(image_data, bs, partition_offset)

        return build_fat_tree(
            image_data, bs, fat, partition_offset,
            include_deleted=include_deleted,
            progress_callback=progress_callback,
        )

    def get_cluster_size(self) -> int:
        return self._cluster_size

    def get_label(self) -> str:
        return self._fs_type
