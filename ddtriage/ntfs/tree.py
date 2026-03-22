"""Build a directory tree from parsed MFT records."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .attributes import (
    AttrType, FileNamespace, ParsedAttribute, FileName,
    NonResidentData, parse_attribute_list,
    ATTR_FLAG_COMPRESSED, ATTR_FLAG_ENCRYPTED, ATTR_FLAG_SPARSE,
)
from .mft_parser import MftRecord

log = logging.getLogger(__name__)

# MFT entries 0-15 are system metadata; entry 5 is the root directory "."
ROOT_MFT_INDEX = 5
SYSTEM_ENTRY_MAX = 15


@dataclass
class FileRecord:
    """A file or directory reconstructed from one or more MFT records."""
    mft_index: int
    name: str
    parent_mft_index: int
    is_directory: bool
    is_deleted: bool
    size: int
    data_runs: list[tuple[int | None, int]]  # [(lcn, cluster_count), ...]
    resident_data: bytes | None
    created: datetime | None
    modified: datetime | None
    is_compressed: bool = False
    is_encrypted: bool = False
    children: list[FileRecord] = field(default_factory=list)

    @property
    def full_path(self) -> str:
        """Compute full path by walking parents. Requires tree to be built."""
        parts: list[str] = []
        node: FileRecord | None = self
        visited: set[int] = set()
        while node is not None and node.mft_index not in visited:
            visited.add(node.mft_index)
            if node.mft_index == ROOT_MFT_INDEX:
                break
            parts.append(node.name)
            node = getattr(node, '_parent', None)
        parts.reverse()
        return '/' + '/'.join(parts)


@dataclass
class DirectoryTree:
    """Complete directory tree built from MFT."""
    root: FileRecord
    orphans: list[FileRecord] = field(default_factory=list)
    all_records: dict[int, FileRecord] = field(default_factory=dict)
    total_files: int = 0
    total_dirs: int = 0


def _pick_best_filename(attrs: list[ParsedAttribute]) -> FileName | None:
    """Pick the best $FILE_NAME attribute.

    Preference: Win32 (1) or Win32+DOS (3) > POSIX (0) > DOS (2).
    """
    fn_attrs = [a.file_name for a in attrs
                if a.header.type == AttrType.FILE_NAME and a.file_name is not None]
    if not fn_attrs:
        return None

    # Sort by preference: namespace 1 and 3 first, then 0, then 2
    priority = {FileNamespace.WIN32: 0, FileNamespace.WIN32_AND_DOS: 0,
                FileNamespace.POSIX: 1, FileNamespace.DOS: 2}
    fn_attrs.sort(key=lambda fn: priority.get(fn.namespace, 3))
    return fn_attrs[0]


def _extract_data_info(
    attrs: list[ParsedAttribute],
) -> tuple[list[tuple[int | None, int]], bytes | None, int, bool, bool]:
    """Extract data runs, resident data, real size, and flags from $DATA attributes.

    Returns (data_runs, resident_data, real_size, is_compressed, is_encrypted).
    Only considers the unnamed (default) $DATA stream.
    """
    for attr in attrs:
        if attr.header.type != AttrType.DATA:
            continue
        if attr.header.name:
            continue  # skip named alternate data streams

        compressed = attr.header.is_compressed
        encrypted = attr.header.is_encrypted

        if attr.non_resident:
            return (attr.non_resident.data_runs,
                    None,
                    attr.non_resident.real_size,
                    compressed, encrypted)
        elif attr.resident:
            return ([], attr.resident.data, attr.resident.data_length,
                    compressed, encrypted)

    return ([], None, 0, False, False)


def _collect_attribute_list_refs(record: MftRecord) -> list[int]:
    """Get MFT record numbers referenced by $ATTRIBUTE_LIST entries."""
    refs: list[int] = []
    for attr in record.get_attributes(AttrType.ATTRIBUTE_LIST):
        if attr.attribute_list_entries:
            for entry in attr.attribute_list_entries:
                if entry.mft_record_number != record.index:
                    refs.append(entry.mft_record_number)
    return refs


def build_tree(
    records: list[MftRecord],
    include_system: bool = False,
    include_deleted: bool = False,
) -> DirectoryTree:
    """Build a DirectoryTree from parsed MFT records.

    Args:
        records: List of parsed MftRecord objects.
        include_system: If True, include system entries (MFT 0-15) in the tree.
        include_deleted: If True, include deleted entries (shown separately).
    """
    # Index all records by MFT number
    records_by_index: dict[int, MftRecord] = {}
    for rec in records:
        records_by_index[rec.index] = rec

    # --- First pass: merge extension records into base records ---
    # Extension records have base_record_index != 0
    extension_attrs: dict[int, list[ParsedAttribute]] = {}  # base_index -> extra attrs
    for rec in records:
        if not rec.is_base_record and rec.in_use:
            base_idx = rec.base_record_index
            if base_idx not in extension_attrs:
                extension_attrs[base_idx] = []
            extension_attrs[base_idx].extend(rec.attributes)

    # Also resolve $ATTRIBUTE_LIST references for base records
    for rec in records:
        if rec.is_base_record and rec.in_use:
            ext_refs = _collect_attribute_list_refs(rec)
            for ref_idx in ext_refs:
                ext_rec = records_by_index.get(ref_idx)
                if ext_rec and ext_rec.index != rec.index:
                    if rec.index not in extension_attrs:
                        extension_attrs[rec.index] = []
                    # Avoid duplicates (extension records already added above)
                    existing_ids = {(a.header.type, a.header.attribute_id)
                                    for a in extension_attrs[rec.index]}
                    for attr in ext_rec.attributes:
                        key = (attr.header.type, attr.header.attribute_id)
                        if key not in existing_ids:
                            extension_attrs[rec.index].append(attr)
                            existing_ids.add(key)

    # --- Second pass: build FileRecord for each base record ---
    file_records: dict[int, FileRecord] = {}
    total_files = 0
    total_dirs = 0

    for rec in records:
        if not rec.is_base_record:
            continue
        if not rec.in_use and not include_deleted:
            continue
        if rec.index <= SYSTEM_ENTRY_MAX and rec.index != ROOT_MFT_INDEX and not include_system:
            continue

        # Combine base attrs + extension attrs
        all_attrs = list(rec.attributes)
        if rec.index in extension_attrs:
            all_attrs.extend(extension_attrs[rec.index])

        fn = _pick_best_filename(all_attrs)
        if fn is None:
            if rec.index == ROOT_MFT_INDEX:
                # Root directory may not have a standard filename
                fn_name = '.'
                parent_idx = ROOT_MFT_INDEX
            else:
                log.debug("MFT %d: no $FILE_NAME, skipping", rec.index)
                continue
        else:
            fn_name = fn.name
            parent_idx = fn.parent_mft_index

        data_runs, resident_data, size, compressed, encrypted = _extract_data_info(all_attrs)

        # Get timestamps from $STANDARD_INFORMATION if available
        created = None
        modified = None
        for attr in all_attrs:
            if attr.standard_info:
                created = attr.standard_info.created
                modified = attr.standard_info.modified
                break
        # Fall back to $FILE_NAME timestamps
        if fn and not created:
            created = fn.created
        if fn and not modified:
            modified = fn.modified

        is_deleted = not rec.in_use

        fr = FileRecord(
            mft_index=rec.index,
            name=fn_name,
            parent_mft_index=parent_idx,
            is_directory=rec.is_directory,
            is_deleted=is_deleted,
            size=size,
            data_runs=data_runs,
            resident_data=resident_data,
            created=created,
            modified=modified,
            is_compressed=compressed,
            is_encrypted=encrypted,
        )
        file_records[rec.index] = fr

        if rec.is_directory:
            total_dirs += 1
        else:
            total_files += 1

    # --- Third pass: link children to parents ---
    orphans: list[FileRecord] = []
    root_record = file_records.get(ROOT_MFT_INDEX)

    if root_record is None:
        # Create a synthetic root if missing
        root_record = FileRecord(
            mft_index=ROOT_MFT_INDEX,
            name='.',
            parent_mft_index=ROOT_MFT_INDEX,
            is_directory=True,
            is_deleted=False,
            size=0,
            data_runs=[],
            resident_data=None,
            created=None,
            modified=None,
        )
        file_records[ROOT_MFT_INDEX] = root_record

    for fr in file_records.values():
        if fr.mft_index == ROOT_MFT_INDEX:
            continue
        parent = file_records.get(fr.parent_mft_index)
        if parent is not None and parent.is_directory:
            parent.children.append(fr)
            fr._parent = parent  # type: ignore[attr-defined]
        else:
            orphans.append(fr)
            fr._parent = None  # type: ignore[attr-defined]

    root_record._parent = None  # type: ignore[attr-defined]

    return DirectoryTree(
        root=root_record,
        orphans=orphans,
        all_records=file_records,
        total_files=total_files,
        total_dirs=total_dirs,
    )
