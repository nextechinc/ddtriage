"""Tests for file extraction from image."""

import os
import struct
import tempfile
import pytest
from pathlib import Path

from ddtriage.mapfile.parser import parse_mapfile
from ddtriage.ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX
from ddtriage.recovery.extractor import (
    extract_file, extract_selected, write_report, _resolve_output_path,
)


CLUSTER_SIZE = 4096
PARTITION_OFFSET = 0


def _make_record(
    mft_index: int, name: str, parent: int = ROOT_MFT_INDEX,
    data_runs: list | None = None, resident_data: bytes | None = None,
    size: int = 0, is_directory: bool = False,
) -> FileRecord:
    rec = FileRecord(
        mft_index=mft_index, name=name, parent_mft_index=parent,
        is_directory=is_directory, is_deleted=False, size=size,
        data_runs=data_runs or [], resident_data=resident_data,
        created=None, modified=None,
    )
    rec._parent = None  # type: ignore[attr-defined]
    return rec


def _build_tree_with_parent(records: list[FileRecord]) -> DirectoryTree:
    root = _make_record(ROOT_MFT_INDEX, ".", is_directory=True)
    root._parent = None  # type: ignore[attr-defined]
    all_records = {ROOT_MFT_INDEX: root}
    for r in records:
        all_records[r.mft_index] = r
        root.children.append(r)
        r._parent = root  # type: ignore[attr-defined]
    return DirectoryTree(
        root=root, orphans=[], all_records=all_records,
        total_files=sum(1 for r in records if not r.is_directory),
        total_dirs=1,
    )


class TestExtractResident:
    def test_resident_file(self):
        content = b"Hello, tiny file!"
        rec = _make_record(16, "tiny.txt", resident_data=content, size=len(content))

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            result = extract_file(rec, "", output_dir, CLUSTER_SIZE, PARTITION_OFFSET)

            assert result.complete is True
            assert result.bytes_written == len(content)
            assert result.error is None

            written = Path(result.output_path).read_bytes()
            assert written == content

    def test_resident_truncated_to_size(self):
        # resident_data may be padded; size field is authoritative
        content = b"Hello, world!!!!!"
        rec = _make_record(16, "trunc.txt", resident_data=content, size=5)

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            result = extract_file(rec, "", output_dir, CLUSTER_SIZE, PARTITION_OFFSET)

            written = Path(result.output_path).read_bytes()
            assert written == b"Hello"


class TestExtractNonResident:
    def test_single_run(self):
        # Create a fake image with known data at LCN 0
        file_data = b"A" * 4096 + b"B" * 4096  # 2 clusters
        rec = _make_record(16, "test.bin", data_runs=[(0, 2)], size=8192)

        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "test.img"
            image_path.write_bytes(file_data + b'\x00' * 4096)

            output_dir = Path(td) / "out"
            result = extract_file(
                rec, str(image_path), output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET,
            )

            assert result.complete is True
            assert result.bytes_written == 8192
            written = Path(result.output_path).read_bytes()
            assert written == file_data

    def test_file_size_trim(self):
        # File is 5000 bytes but occupies 2 clusters (8192 bytes)
        file_data = b"X" * 8192
        rec = _make_record(16, "sized.bin", data_runs=[(0, 2)], size=5000)

        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "test.img"
            image_path.write_bytes(file_data)

            output_dir = Path(td) / "out"
            result = extract_file(
                rec, str(image_path), output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET,
            )

            written = Path(result.output_path).read_bytes()
            assert len(written) == 5000

    def test_sparse_run(self):
        # First run is sparse (zeros), second is real data
        real_data = b"R" * 4096
        rec = _make_record(16, "sparse.bin",
                           data_runs=[(None, 2), (0, 1)], size=12288)

        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "test.img"
            image_path.write_bytes(real_data + b'\x00' * 4096)

            output_dir = Path(td) / "out"
            result = extract_file(
                rec, str(image_path), output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET,
            )

            written = Path(result.output_path).read_bytes()
            assert written[:8192] == b'\x00' * 8192  # sparse
            assert written[8192:] == real_data         # real

    def test_gap_detection(self):
        """Clusters not rescued should be flagged as gaps."""
        mapfile_text = "0x00000000  +\n0x00000000  0x00001000  ?\n"
        mf = parse_mapfile(mapfile_text)

        rec = _make_record(16, "gapped.bin", data_runs=[(0, 1)], size=4096)

        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "test.img"
            image_path.write_bytes(b'\x00' * 4096)

            output_dir = Path(td) / "out"
            result = extract_file(
                rec, str(image_path), output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET, mf,
            )

            assert len(result.gaps) == 1
            assert result.complete is False

    def test_empty_file(self):
        rec = _make_record(16, "empty.txt", size=0)

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            result = extract_file(rec, "", output_dir, CLUSTER_SIZE, PARTITION_OFFSET)

            assert result.complete is True
            written = Path(result.output_path).read_bytes()
            assert written == b""


class TestExtractSelected:
    def test_multiple_files(self):
        f1 = _make_record(16, "a.txt", resident_data=b"aaa", size=3)
        f2 = _make_record(17, "b.txt", resident_data=b"bbb", size=3)
        tree = _build_tree_with_parent([f1, f2])

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            results = extract_selected(
                {16, 17}, tree, "", output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET,
            )

            assert len(results) == 2
            assert all(r.complete for r in results)

    def test_skips_directories(self):
        d = _make_record(16, "mydir", is_directory=True)
        f = _make_record(17, "file.txt", parent=16, resident_data=b"x", size=1)
        tree = _build_tree_with_parent([d, f])

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            results = extract_selected(
                {16, 17}, tree, "", output_dir,
                CLUSTER_SIZE, PARTITION_OFFSET,
            )

            # Only the file, not the directory
            assert len(results) == 1
            assert results[0].mft_index == 17


class TestDirectoryStructure:
    def test_nested_dirs_created(self):
        root = _make_record(ROOT_MFT_INDEX, ".", is_directory=True)
        docs = _make_record(16, "Documents", parent=ROOT_MFT_INDEX, is_directory=True)
        f1 = _make_record(17, "report.txt", parent=16, resident_data=b"data", size=4)

        docs._parent = root  # type: ignore[attr-defined]
        f1._parent = docs  # type: ignore[attr-defined]
        root._parent = None  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "out"
            result = extract_file(f1, "", output_dir, CLUSTER_SIZE, PARTITION_OFFSET)

            assert result.complete is True
            assert (output_dir / "Documents" / "report.txt").exists()
            assert (output_dir / "Documents" / "report.txt").read_bytes() == b"data"


class TestWriteReport:
    def test_report_created(self):
        from ddtriage.recovery.extractor import ExtractionResult

        results = [
            ExtractionResult(16, "ok.txt", "/ok.txt", "/out/ok.txt",
                             100, 100, True),
            ExtractionResult(17, "bad.txt", "/bad.txt", "/out/bad.txt",
                             200, 0, False, error="No data runs"),
        ]

        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            write_report(results, output_dir)

            report = (output_dir / "recovery_report.txt").read_text()
            assert "Complete:  1" in report
            assert "Failed:    1" in report
            assert "ok.txt" in report
            assert "bad.txt" in report
