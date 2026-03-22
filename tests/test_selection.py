"""Tests for selection export/import."""

import json
import os
import tempfile
import pytest

from ddtriage.selection import (
    export_selection, import_selection, collect_selection_with_children,
)
from ddtriage.ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX


def _make_record(
    mft_index: int,
    name: str,
    parent: int = ROOT_MFT_INDEX,
    is_directory: bool = False,
    size: int = 0,
) -> FileRecord:
    return FileRecord(
        mft_index=mft_index,
        name=name,
        parent_mft_index=parent,
        is_directory=is_directory,
        is_deleted=False,
        size=size,
        data_runs=[],
        resident_data=None,
        created=None,
        modified=None,
    )


def _build_test_tree() -> DirectoryTree:
    """Build a small tree: root > docs(dir) > [file1, file2], root > file3."""
    root = _make_record(ROOT_MFT_INDEX, ".", is_directory=True)
    docs = _make_record(16, "Documents", parent=ROOT_MFT_INDEX, is_directory=True)
    f1 = _make_record(17, "file1.txt", parent=16, size=100)
    f2 = _make_record(18, "file2.txt", parent=16, size=200)
    f3 = _make_record(19, "file3.txt", parent=ROOT_MFT_INDEX, size=300)

    docs.children = [f1, f2]
    root.children = [docs, f3]

    # Set _parent for full_path
    f1._parent = docs  # type: ignore[attr-defined]
    f2._parent = docs  # type: ignore[attr-defined]
    f3._parent = root  # type: ignore[attr-defined]
    docs._parent = root  # type: ignore[attr-defined]
    root._parent = None  # type: ignore[attr-defined]

    all_records = {r.mft_index: r for r in [root, docs, f1, f2, f3]}
    return DirectoryTree(
        root=root, orphans=[], all_records=all_records,
        total_files=3, total_dirs=2,
    )


class TestExportImport:
    def test_round_trip(self):
        tree = _build_test_tree()
        selected = {17, 19}

        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            path = f.name
        try:
            export_selection(selected, tree, path)

            # Verify JSON structure
            with open(path) as f:
                data = json.load(f)
            assert data["version"] == 1
            assert data["total_selected"] == 2
            assert data["total_size"] == 400  # 100 + 300

            entries = {e["mft_index"] for e in data["entries"]}
            assert entries == {17, 19}

            # Import back
            loaded = import_selection(path)
            assert loaded == {17, 19}
        finally:
            os.unlink(path)

    def test_import_bad_version(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            json.dump({"version": 99, "entries": []}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="Unsupported"):
                import_selection(path)
        finally:
            os.unlink(path)

    def test_export_includes_paths(self):
        tree = _build_test_tree()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            path = f.name
        try:
            export_selection({17}, tree, path)
            with open(path) as f:
                data = json.load(f)
            assert data["entries"][0]["path"] == "/Documents/file1.txt"
        finally:
            os.unlink(path)


class TestCollectWithChildren:
    def test_file_only(self):
        tree = _build_test_tree()
        result = collect_selection_with_children({17}, tree)
        assert result == {17}

    def test_directory_expands(self):
        tree = _build_test_tree()
        # Selecting Documents should include it + file1 + file2
        result = collect_selection_with_children({16}, tree)
        assert result == {16, 17, 18}

    def test_root_expands_all(self):
        tree = _build_test_tree()
        result = collect_selection_with_children({ROOT_MFT_INDEX}, tree)
        assert result == {ROOT_MFT_INDEX, 16, 17, 18, 19}

    def test_missing_index_ignored(self):
        tree = _build_test_tree()
        result = collect_selection_with_children({999}, tree)
        assert result == set()
