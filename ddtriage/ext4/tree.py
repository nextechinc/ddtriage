"""Build a directory tree from an ext4 filesystem."""

from __future__ import annotations

import logging

from ..ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX
from .superblock import ExtSuperblock
from .group_desc import GroupDescriptor, parse_group_descriptors
from .inode import parse_inode, Inode, inode_to_data_runs, S_IFDIR
from .dir_entry import parse_directory_block, DirEntry, FT_DIR

log = logging.getLogger(__name__)

ROOT_INODE_NUM = 2


def _read_inode(
    image_data,
    sb: ExtSuperblock,
    descriptors: list[GroupDescriptor],
    inode_num: int,
    partition_offset: int,
) -> Inode | None:
    """Read inode N from the image."""
    if inode_num < 1:
        return None

    group = (inode_num - 1) // sb.inodes_per_group
    index_in_group = (inode_num - 1) % sb.inodes_per_group

    if group >= len(descriptors):
        return None

    inode_table_block = descriptors[group].inode_table
    offset = partition_offset + inode_table_block * sb.block_size + index_in_group * sb.inode_size

    if offset + sb.inode_size > len(image_data):
        return None

    try:
        return parse_inode(bytes(image_data[offset:offset + sb.inode_size]))
    except ValueError:
        return None


def _read_directory_data(
    image_data,
    inode: Inode,
    block_size: int,
    partition_offset: int,
    max_size: int | None = None,
) -> bytes:
    """Read a directory's data blocks using its inode's data runs."""
    runs = inode_to_data_runs(inode, image_data, block_size, partition_offset)
    result = bytearray()

    for lcn, count in runs:
        if lcn is None:
            # Sparse — shouldn't happen in directories, but pad with zeros
            result.extend(b'\x00' * count * block_size)
            continue

        byte_offset = partition_offset + lcn * block_size
        byte_length = count * block_size
        end = byte_offset + byte_length

        if end > len(image_data):
            result.extend(image_data[byte_offset:len(image_data)])
        else:
            result.extend(image_data[byte_offset:end])

        if max_size and len(result) >= max_size:
            break

    # Trim to reported file size if known
    if inode.size > 0:
        return bytes(result[:inode.size])
    return bytes(result)


def build_ext4_tree(
    image_data,
    sb: ExtSuperblock,
    descriptors: list[GroupDescriptor],
    partition_offset: int,
    include_deleted: bool = False,
    progress_callback=None,
) -> DirectoryTree:
    """Build a DirectoryTree from an ext4 filesystem."""
    file_records: dict[int, FileRecord] = {}
    total_files = 0
    total_dirs = 0
    entries_processed = 0
    visited_inodes: set[int] = set()

    # Read root inode (inode 2)
    root_inode = _read_inode(image_data, sb, descriptors, ROOT_INODE_NUM, partition_offset)
    if root_inode is None:
        raise ValueError("Could not read root inode (inode 2)")

    # We use inode numbers directly as record IDs (ext4 inodes are unique)
    root_record = FileRecord(
        mft_index=ROOT_INODE_NUM,
        name='.',
        parent_mft_index=ROOT_INODE_NUM,
        is_directory=True,
        is_deleted=False,
        size=0,
        data_runs=[],
        resident_data=None,
        created=root_inode.created,
        modified=root_inode.modified,
    )
    root_record._parent = None  # type: ignore[attr-defined]
    file_records[ROOT_INODE_NUM] = root_record

    def _process_dir(parent_record: FileRecord, inode: Inode, depth: int = 0) -> None:
        nonlocal total_files, total_dirs, entries_processed

        if depth > 64 or parent_record.mft_index in visited_inodes:
            return
        visited_inodes.add(parent_record.mft_index)

        dir_data = _read_directory_data(
            image_data, inode, sb.block_size, partition_offset,
            max_size=16 * 1024 * 1024,
        )
        if not dir_data:
            return

        # Parse block by block (entries don't cross block boundaries)
        entries: list[DirEntry] = []
        for block_start in range(0, len(dir_data), sb.block_size):
            block = dir_data[block_start:block_start + sb.block_size]
            entries.extend(parse_directory_block(block, has_filetype=sb.has_filetype))

        for entry in entries:
            entries_processed += 1
            if progress_callback and entries_processed % 500 == 0:
                progress_callback(entries_processed, None)

            if entry.inode in file_records:
                continue  # avoid duplicates (hard links)

            child_inode = _read_inode(
                image_data, sb, descriptors, entry.inode, partition_offset,
            )
            if child_inode is None:
                continue

            if child_inode.is_deleted and not include_deleted:
                continue

            is_dir = child_inode.is_directory or entry.file_type == FT_DIR

            if is_dir:
                data_runs = []
                size = 0
            else:
                data_runs = inode_to_data_runs(
                    child_inode, image_data, sb.block_size, partition_offset,
                )
                size = child_inode.size

            rec = FileRecord(
                mft_index=entry.inode,
                name=entry.name,
                parent_mft_index=parent_record.mft_index,
                is_directory=is_dir,
                is_deleted=child_inode.is_deleted,
                size=size,
                data_runs=data_runs,
                resident_data=None,
                created=child_inode.created,
                modified=child_inode.modified,
            )
            rec._parent = parent_record  # type: ignore[attr-defined]
            parent_record.children.append(rec)
            file_records[entry.inode] = rec

            if is_dir:
                total_dirs += 1
                _process_dir(rec, child_inode, depth + 1)
            else:
                total_files += 1

    _process_dir(root_record, root_inode)

    return DirectoryTree(
        root=root_record,
        orphans=[],
        all_records=file_records,
        total_files=total_files,
        total_dirs=total_dirs,
    )
