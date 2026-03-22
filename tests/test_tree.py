"""Tests for directory tree construction from MFT records."""

import struct
import pytest

from ddtriage.ntfs.mft_parser import MftRecord
from ddtriage.ntfs.attributes import (
    AttrType, FileNamespace, ParsedAttribute, AttributeHeader,
    ResidentData, NonResidentData, FileName, StandardInformation,
    AttributeListEntry,
)
from ddtriage.ntfs.tree import build_tree, FileRecord, DirectoryTree, ROOT_MFT_INDEX


def _make_filename_attr(
    name: str,
    parent_mft: int = ROOT_MFT_INDEX,
    namespace: int = FileNamespace.WIN32_AND_DOS,
    real_size: int = 0,
) -> ParsedAttribute:
    """Create a minimal ParsedAttribute with $FILE_NAME data."""
    fn = FileName(
        parent_reference=parent_mft | (1 << 48),
        parent_mft_index=parent_mft,
        parent_sequence=1,
        created=None, modified=None, mft_modified=None, accessed=None,
        allocated_size=real_size, real_size=real_size,
        flags=0, name_length=len(name), namespace=namespace,
        name=name,
    )
    header = AttributeHeader(
        type=AttrType.FILE_NAME, length=0, non_resident=False,
        name_length=0, name_offset=0, flags=0, attribute_id=0, name="",
    )
    return ParsedAttribute(
        header=header,
        resident=ResidentData(data_length=0, data_offset=0, data=b''),
        file_name=fn,
    )


def _make_data_attr_resident(data: bytes) -> ParsedAttribute:
    """Create a resident $DATA attribute."""
    header = AttributeHeader(
        type=AttrType.DATA, length=0, non_resident=False,
        name_length=0, name_offset=0, flags=0, attribute_id=1, name="",
    )
    return ParsedAttribute(
        header=header,
        resident=ResidentData(data_length=len(data), data_offset=0, data=data),
    )


def _make_data_attr_nonresident(
    runs: list[tuple[int | None, int]], real_size: int = 0,
) -> ParsedAttribute:
    """Create a non-resident $DATA attribute."""
    header = AttributeHeader(
        type=AttrType.DATA, length=0, non_resident=True,
        name_length=0, name_offset=0, flags=0, attribute_id=1, name="",
    )
    return ParsedAttribute(
        header=header,
        non_resident=NonResidentData(
            start_vcn=0, end_vcn=0, data_runs_offset=0,
            allocated_size=real_size, real_size=real_size,
            initialized_size=real_size, data_runs=runs,
        ),
    )


def _make_std_info() -> ParsedAttribute:
    """Create a $STANDARD_INFORMATION attribute."""
    header = AttributeHeader(
        type=AttrType.STANDARD_INFORMATION, length=0, non_resident=False,
        name_length=0, name_offset=0, flags=0, attribute_id=0, name="",
    )
    return ParsedAttribute(
        header=header,
        standard_info=StandardInformation(
            created=None, modified=None, mft_modified=None, accessed=None, flags=0,
        ),
    )


def _make_record(
    index: int, flags: int = 0x01, attrs: list[ParsedAttribute] | None = None,
    base_ref: int = 0,
) -> MftRecord:
    return MftRecord(
        index=index, signature=b'FILE', sequence_number=1,
        hard_link_count=1, flags=flags, used_size=1024, allocated_size=1024,
        base_record_reference=base_ref,
        base_record_index=base_ref & 0x0000FFFFFFFFFFFF,
        attributes=attrs or [],
    )


class TestBuildTree:
    def test_simple_tree(self):
        """Root directory with two files."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        f1 = _make_record(16, attrs=[
            _make_filename_attr("hello.txt", parent_mft=5),
            _make_data_attr_resident(b"Hello"),
        ])
        f2 = _make_record(17, attrs=[
            _make_filename_attr("world.txt", parent_mft=5),
            _make_data_attr_nonresident([(100, 5)], real_size=20480),
        ])

        tree = build_tree([root, f1, f2])

        assert tree.root.mft_index == 5
        assert len(tree.root.children) == 2
        assert tree.total_files == 2
        assert tree.total_dirs == 1

        names = {c.name for c in tree.root.children}
        assert names == {"hello.txt", "world.txt"}

    def test_nested_directories(self):
        """Root > Documents > file.txt"""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        docs = _make_record(16, flags=0x03, attrs=[
            _make_filename_attr("Documents", parent_mft=5),
        ])
        f1 = _make_record(17, attrs=[
            _make_filename_attr("file.txt", parent_mft=16),
            _make_data_attr_resident(b"data"),
        ])

        tree = build_tree([root, docs, f1])

        assert len(tree.root.children) == 1
        assert tree.root.children[0].name == "Documents"
        assert len(tree.root.children[0].children) == 1
        assert tree.root.children[0].children[0].name == "file.txt"

    def test_orphan_handling(self):
        """File whose parent doesn't exist becomes orphan."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        orphan = _make_record(20, attrs=[
            _make_filename_attr("lost.txt", parent_mft=999),
            _make_data_attr_resident(b"lost"),
        ])

        tree = build_tree([root, orphan])

        assert len(tree.root.children) == 0
        assert len(tree.orphans) == 1
        assert tree.orphans[0].name == "lost.txt"

    def test_system_entries_excluded(self):
        """MFT entries 0-15 (except 5) are excluded by default."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        mft = _make_record(0, attrs=[
            _make_filename_attr("$MFT", parent_mft=5),
        ])
        bitmap = _make_record(6, attrs=[
            _make_filename_attr("$Bitmap", parent_mft=5),
        ])
        user_file = _make_record(16, attrs=[
            _make_filename_attr("user.txt", parent_mft=5),
            _make_data_attr_resident(b"x"),
        ])

        tree = build_tree([root, mft, bitmap, user_file])

        names = {c.name for c in tree.root.children}
        assert "$MFT" not in names
        assert "$Bitmap" not in names
        assert "user.txt" in names

    def test_system_entries_included(self):
        """include_system=True shows system entries."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        mft = _make_record(0, attrs=[
            _make_filename_attr("$MFT", parent_mft=5),
        ])

        tree = build_tree([root, mft], include_system=True)
        names = {c.name for c in tree.root.children}
        assert "$MFT" in names

    def test_prefer_win32_filename(self):
        """Win32 name preferred over DOS 8.3 name."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        f1 = _make_record(16, attrs=[
            _make_filename_attr("LONGFI~1.TXT", parent_mft=5, namespace=FileNamespace.DOS),
            _make_filename_attr("long_filename.txt", parent_mft=5, namespace=FileNamespace.WIN32),
            _make_data_attr_resident(b"data"),
        ])

        tree = build_tree([root, f1])
        assert tree.root.children[0].name == "long_filename.txt"

    def test_deleted_entries_excluded(self):
        """Deleted entries excluded by default."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        alive = _make_record(16, flags=0x01, attrs=[
            _make_filename_attr("alive.txt", parent_mft=5),
            _make_data_attr_resident(b"x"),
        ])
        dead = _make_record(17, flags=0x00, attrs=[
            _make_filename_attr("dead.txt", parent_mft=5),
            _make_data_attr_resident(b"y"),
        ])

        tree = build_tree([root, alive, dead])
        names = {c.name for c in tree.root.children}
        assert "alive.txt" in names
        assert "dead.txt" not in names

    def test_deleted_entries_included(self):
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        dead = _make_record(17, flags=0x00, attrs=[
            _make_filename_attr("dead.txt", parent_mft=5),
            _make_data_attr_resident(b"y"),
        ])

        tree = build_tree([root, dead], include_deleted=True)
        assert len(tree.root.children) == 1
        assert tree.root.children[0].is_deleted is True

    def test_resident_data_preserved(self):
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        f1 = _make_record(16, attrs=[
            _make_filename_attr("tiny.txt", parent_mft=5),
            _make_data_attr_resident(b"Hello, tiny!"),
        ])

        tree = build_tree([root, f1])
        child = tree.root.children[0]
        assert child.resident_data == b"Hello, tiny!"
        assert child.size == 12

    def test_nonresident_data_runs_preserved(self):
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        runs = [(100, 10), (200, 5)]
        f1 = _make_record(16, attrs=[
            _make_filename_attr("big.bin", parent_mft=5),
            _make_data_attr_nonresident(runs, real_size=61440),
        ])

        tree = build_tree([root, f1])
        child = tree.root.children[0]
        assert child.data_runs == runs
        assert child.size == 61440
        assert child.resident_data is None

    def test_extension_record_merging(self):
        """Extension records should merge attributes into the base record."""
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        # Base record has filename but data runs are in extension
        base = _make_record(16, attrs=[
            _make_filename_attr("fragmented.bin", parent_mft=5),
            # $ATTRIBUTE_LIST pointing to extension record 100
            ParsedAttribute(
                header=AttributeHeader(
                    type=AttrType.ATTRIBUTE_LIST, length=0, non_resident=False,
                    name_length=0, name_offset=0, flags=0, attribute_id=0, name="",
                ),
                resident=ResidentData(data_length=0, data_offset=0, data=b''),
                attribute_list_entries=[
                    AttributeListEntry(
                        attr_type=AttrType.DATA, record_length=26,
                        name_length=0, name_offset=0, start_vcn=0,
                        mft_reference=100 | (1 << 48),
                        mft_record_number=100, mft_sequence=1,
                        attribute_id=1, name="",
                    ),
                ],
            ),
        ])
        # Extension record with the $DATA attribute
        ext = _make_record(100, flags=0x01, base_ref=16 | (1 << 48), attrs=[
            _make_data_attr_nonresident([(500, 20)], real_size=81920),
        ])

        tree = build_tree([root, base, ext])
        assert len(tree.root.children) == 1
        child = tree.root.children[0]
        assert child.name == "fragmented.bin"
        assert child.data_runs == [(500, 20)]
        assert child.size == 81920

    def test_synthetic_root_when_missing(self):
        """Tree should still work if MFT entry 5 is damaged/missing."""
        f1 = _make_record(16, attrs=[
            _make_filename_attr("file.txt", parent_mft=5),
            _make_data_attr_resident(b"x"),
        ])

        tree = build_tree([f1])
        assert tree.root.mft_index == ROOT_MFT_INDEX
        assert tree.root.name == "."
        assert len(tree.root.children) == 1

    def test_full_path(self):
        root = _make_record(5, flags=0x03, attrs=[
            _make_filename_attr(".", parent_mft=5),
        ])
        docs = _make_record(16, flags=0x03, attrs=[
            _make_filename_attr("Documents", parent_mft=5),
        ])
        f1 = _make_record(17, attrs=[
            _make_filename_attr("report.pdf", parent_mft=16),
            _make_data_attr_resident(b"pdf"),
        ])

        tree = build_tree([root, docs, f1])
        report = tree.all_records[17]
        assert report.full_path == "/Documents/report.pdf"
