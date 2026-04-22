"""Build a directory tree from a FAT filesystem."""

from __future__ import annotations

import logging
from dataclasses import field

from ..ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX
from .boot_sector import FATBootSector
from .fat_table import FATTable
from .dir_entry import parse_directory, FATDirEntry

log = logging.getLogger(__name__)


def _read_cluster_chain(
    image_data,
    fat: FATTable,
    bs: FATBootSector,
    start_cluster: int,
    partition_offset: int,
    max_size: int | None = None,
) -> bytes:
    """Read data from a cluster chain."""
    result = bytearray()
    runs = fat.follow_chain(start_cluster)

    for cluster, count in runs:
        byte_offset = partition_offset + bs.cluster_to_offset(cluster)
        byte_length = count * bs.cluster_size
        end = byte_offset + byte_length

        if end > len(image_data):
            chunk = bytes(image_data[byte_offset:len(image_data)])
        else:
            chunk = bytes(image_data[byte_offset:end])

        result.extend(chunk)

        if max_size and len(result) >= max_size:
            break

    return bytes(result)


def build_fat_tree(
    image_data,
    bs: FATBootSector,
    fat: FATTable,
    partition_offset: int,
    include_deleted: bool = False,
    progress_callback=None,
) -> DirectoryTree:
    """Build a DirectoryTree from a FAT filesystem.

    Recursively reads directories starting from the root.
    """
    file_records: dict[int, FileRecord] = {}
    next_id = ROOT_MFT_INDEX  # start IDs from 5 to match NTFS convention
    total_files = 0
    total_dirs = 0
    entries_processed = 0

    def _alloc_id() -> int:
        nonlocal next_id
        idx = next_id
        next_id += 1
        return idx

    # Create root record
    root_id = _alloc_id()
    root = FileRecord(
        mft_index=root_id,
        name='.',
        parent_mft_index=root_id,
        is_directory=True,
        is_deleted=False,
        size=0,
        data_runs=[],
        resident_data=None,
        created=None,
        modified=None,
    )
    root._parent = None  # type: ignore[attr-defined]
    file_records[root_id] = root

    def _process_dir(
        parent_record: FileRecord,
        dir_cluster: int,
        depth: int = 0,
    ) -> None:
        nonlocal total_files, total_dirs, entries_processed

        if depth > 64:
            log.warning("Directory depth limit reached at %s", parent_record.name)
            return

        # Read directory data
        if dir_cluster < 2:
            # FAT16/12 root directory (fixed location)
            if bs.root_entry_count > 0:
                root_start = partition_offset + bs.data_start - (
                    bs.root_entry_count * 32
                )
                root_size = bs.root_entry_count * 32
                if root_start + root_size > len(image_data):
                    return
                dir_data = bytes(image_data[root_start:root_start + root_size])
            else:
                return
        else:
            dir_data = _read_cluster_chain(
                image_data, fat, bs, dir_cluster, partition_offset,
                max_size=4 * 1024 * 1024,  # cap at 4MB per directory
            )

        if not dir_data:
            return

        entries = parse_directory(dir_data)

        for entry in entries:
            if entry.is_deleted and not include_deleted:
                continue

            entries_processed += 1
            if progress_callback and entries_processed % 1000 == 0:
                progress_callback(entries_processed, None)

            rec_id = _alloc_id()
            data_runs = fat.chain_to_data_runs(entry.start_cluster) if entry.start_cluster >= 2 else []

            # Small files without cluster chains might have size > 0 but
            # cluster 0 (shouldn't happen in FAT, but handle gracefully)
            resident_data = None

            rec = FileRecord(
                mft_index=rec_id,
                name=entry.name,
                parent_mft_index=parent_record.mft_index,
                is_directory=entry.is_directory,
                is_deleted=entry.is_deleted,
                size=entry.size if not entry.is_directory else 0,
                data_runs=data_runs,
                resident_data=resident_data,
                created=entry.created,
                modified=entry.modified,
            )
            rec._parent = parent_record  # type: ignore[attr-defined]
            parent_record.children.append(rec)
            file_records[rec_id] = rec

            if entry.is_directory:
                total_dirs += 1
                if entry.start_cluster >= 2:
                    _process_dir(rec, entry.start_cluster, depth + 1)
            else:
                total_files += 1

    # Start from root directory
    if bs.fs_type == "FAT32":
        _process_dir(root, bs.root_cluster)
    else:
        # FAT16/12: root directory is at a fixed location
        _process_dir(root, 0)

    return DirectoryTree(
        root=root,
        orphans=[],
        all_records=file_records,
        total_files=total_files,
        total_dirs=total_dirs,
    )
