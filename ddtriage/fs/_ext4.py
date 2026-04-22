"""ext4 filesystem parser adapter."""

from __future__ import annotations

from ..ext4.superblock import parse_superblock
from ..ext4.group_desc import parse_group_descriptors
from ..ext4.tree import build_ext4_tree
from ..ntfs.tree import DirectoryTree
from . import FilesystemParser


class Ext4FilesystemParser(FilesystemParser):
    """Parser for ext2/3/4 filesystems."""

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
        sb = parse_superblock(image_data, partition_offset)
        self._cluster_size = sb.block_size

        descriptors = parse_group_descriptors(image_data, sb, partition_offset)
        if not descriptors:
            raise ValueError("No block group descriptors found")

        return build_ext4_tree(
            image_data, sb, descriptors, partition_offset,
            include_deleted=include_deleted,
            progress_callback=progress_callback,
        )

    def get_cluster_size(self) -> int:
        return self._cluster_size

    def get_label(self) -> str:
        return "ext4"
