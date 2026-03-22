"""Selection export/import — save and load file selections as JSON."""

from __future__ import annotations

import json
from pathlib import Path

from .ntfs.tree import FileRecord, DirectoryTree


def export_selection(
    selected: set[int],
    tree: DirectoryTree,
    output_path: str | Path,
) -> None:
    """Save selected MFT indices and metadata to a JSON file.

    Args:
        selected: Set of MFT indices that are selected.
        tree: The directory tree (for resolving names/paths).
        output_path: Path to write the JSON file.
    """
    entries = []
    for mft_idx in sorted(selected):
        record = tree.all_records.get(mft_idx)
        if record is None:
            continue
        entries.append({
            "mft_index": mft_idx,
            "name": record.name,
            "path": record.full_path,
            "is_directory": record.is_directory,
            "size": record.size,
        })

    data = {
        "version": 1,
        "total_selected": len(entries),
        "total_size": sum(e["size"] for e in entries),
        "entries": entries,
    }

    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)


def import_selection(input_path: str | Path) -> set[int]:
    """Load selected MFT indices from a JSON file.

    Returns a set of MFT indices.
    """
    with open(input_path, 'r') as f:
        data = json.load(f)

    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported selection file version: {version}")

    return {entry["mft_index"] for entry in data.get("entries", [])}


def collect_selection_with_children(
    selected: set[int],
    tree: DirectoryTree,
) -> set[int]:
    """Expand selection: if a directory is selected, include all its descendants."""
    result = set()

    def _walk(record: FileRecord) -> None:
        result.add(record.mft_index)
        for child in record.children:
            _walk(child)

    for mft_idx in selected:
        record = tree.all_records.get(mft_idx)
        if record is None:
            continue
        if record.is_directory:
            _walk(record)
        else:
            result.add(mft_idx)

    return result
