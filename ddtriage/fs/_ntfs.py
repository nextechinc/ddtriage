"""NTFS filesystem parser adapter."""

from __future__ import annotations

from ..ntfs.boot_sector import parse_boot_sector
from ..ntfs.mft_parser import iter_mft_records
from ..ntfs.tree import build_tree, DirectoryTree
from . import FilesystemParser


class NTFSFilesystemParser(FilesystemParser):
    """Adapter wrapping the existing NTFS parser modules."""

    def __init__(self):
        self._cluster_size = 4096
        self._record_size = 1024

    def parse(
        self,
        image_data,
        partition_offset: int,
        progress_callback=None,
        include_deleted: bool = False,
        include_system: bool = False,
    ) -> DirectoryTree:
        bs = parse_boot_sector(image_data, partition_offset)
        self._cluster_size = bs.cluster_size
        self._record_size = bs.mft_record_size
        mft_offset = bs.mft_offset(partition_offset)

        records = iter_mft_records(
            image_data, mft_offset, bs.mft_record_size,
            progress_callback=progress_callback,
        )
        return build_tree(
            records,
            include_system=include_system,
            include_deleted=include_deleted,
        )

    def get_cluster_size(self) -> int:
        return self._cluster_size

    def get_label(self) -> str:
        return "NTFS"
