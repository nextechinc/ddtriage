"""exFAT filesystem parser adapter."""

from __future__ import annotations

from ..exfat.boot_sector import parse_exfat_boot_sector
from ..exfat.fat_table import ExFATTable
from ..exfat.tree import build_exfat_tree
from ..ntfs.tree import DirectoryTree
from . import FilesystemParser


class ExFATFilesystemParser(FilesystemParser):
    """Parser for exFAT filesystems."""

    def __init__(self):
        self._cluster_size = 4096

    def parse(
        self,
        image_data,
        partition_offset: int,
        progress_callback=None,
        include_deleted: bool = False,
        **kwargs,
    ) -> DirectoryTree:
        bs = parse_exfat_boot_sector(image_data, partition_offset)
        self._cluster_size = bs.cluster_size

        fat = ExFATTable(image_data, bs, partition_offset)

        return build_exfat_tree(
            image_data, bs, fat, partition_offset,
            include_deleted=include_deleted,
            progress_callback=progress_callback,
        )

    def get_cluster_size(self) -> int:
        return self._cluster_size

    def get_label(self) -> str:
        return "exFAT"
