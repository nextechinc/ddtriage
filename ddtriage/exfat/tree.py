"""Build a directory tree from an exFAT filesystem."""

from __future__ import annotations

import logging
import math

from ..ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX
from .boot_sector import ExFATBootSector
from .fat_table import ExFATTable
from .dir_entry import parse_directory, ExFATFileEntry

log = logging.getLogger(__name__)


def _read_cluster_chain(
    image_data,
    fat: ExFATTable,
    bs: ExFATBootSector,
    start_cluster: int,
    partition_offset: int,
    no_fat_chain: bool = False,
    data_length: int = 0,
    max_size: int | None = None,
) -> bytes:
    """Read data from a cluster chain or contiguous allocation."""
    result = bytearray()

    if no_fat_chain and start_cluster >= 2:
        # Contiguous — calculate cluster count from data length
        cluster_count = math.ceil(data_length / bs.cluster_size) if data_length > 0 else 1
        byte_offset = partition_offset + bs.cluster_to_offset(start_cluster)
        byte_length = cluster_count * bs.cluster_size
        end = byte_offset + byte_length
        if end > len(image_data):
            result.extend(image_data[byte_offset:len(image_data)])
        else:
            result.extend(image_data[byte_offset:end])
    else:
        # Follow FAT chain
        runs = fat.follow_chain(start_cluster)
        for cluster, count in runs:
            byte_offset = partition_offset + bs.cluster_to_offset(cluster)
            byte_length = count * bs.cluster_size
            end = byte_offset + byte_length
            if end > len(image_data):
                result.extend(image_data[byte_offset:len(image_data)])
            else:
                result.extend(image_data[byte_offset:end])

            if max_size and len(result) >= max_size:
                break

    return bytes(result)


def build_exfat_tree(
    image_data,
    bs: ExFATBootSector,
    fat: ExFATTable,
    partition_offset: int,
    include_deleted: bool = False,
    progress_callback=None,
) -> DirectoryTree:
    """Build a DirectoryTree from an exFAT filesystem."""
    file_records: dict[int, FileRecord] = {}
    next_id = ROOT_MFT_INDEX
    total_files = 0
    total_dirs = 0
    entries_processed = 0

    def _alloc_id() -> int:
        nonlocal next_id
        idx = next_id
        next_id += 1
        return idx

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
        no_fat_chain: bool = False,
        dir_size: int = 0,
        depth: int = 0,
    ) -> None:
        nonlocal total_files, total_dirs, entries_processed

        if depth > 64:
            log.warning("Directory depth limit at %s", parent_record.name)
            return

        if dir_cluster < 2:
            return

        dir_data = _read_cluster_chain(
            image_data, fat, bs, dir_cluster, partition_offset,
            no_fat_chain=no_fat_chain,
            data_length=dir_size or 4 * 1024 * 1024,
            max_size=4 * 1024 * 1024,
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

            if entry.start_cluster >= 2:
                if entry.no_fat_chain:
                    data_runs = fat.contiguous_data_runs(
                        entry.start_cluster,
                        math.ceil(entry.size / bs.cluster_size) if entry.size > 0 else 1,
                    )
                else:
                    data_runs = fat.chain_to_data_runs(entry.start_cluster)
            else:
                data_runs = []

            rec = FileRecord(
                mft_index=rec_id,
                name=entry.name,
                parent_mft_index=parent_record.mft_index,
                is_directory=entry.is_directory,
                is_deleted=entry.is_deleted,
                size=entry.size if not entry.is_directory else 0,
                data_runs=data_runs,
                resident_data=None,
                created=entry.created,
                modified=entry.modified,
            )
            rec._parent = parent_record  # type: ignore[attr-defined]
            parent_record.children.append(rec)
            file_records[rec_id] = rec

            if entry.is_directory:
                total_dirs += 1
                if entry.start_cluster >= 2:
                    _process_dir(
                        rec, entry.start_cluster,
                        no_fat_chain=entry.no_fat_chain,
                        dir_size=entry.size,
                        depth=depth + 1,
                    )
            else:
                total_files += 1

    _process_dir(root, bs.root_cluster)

    return DirectoryTree(
        root=root,
        orphans=[],
        all_records=file_records,
        total_files=total_files,
        total_dirs=total_dirs,
    )
