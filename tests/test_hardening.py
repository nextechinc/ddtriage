"""Tests for Sprint 5 hardening: corrupted records, edge cases."""

import struct
import pytest

from ddtriage.ntfs.mft_parser import parse_mft_record, iter_mft_records, MftRecord
from ddtriage.ntfs.attributes import (
    parse_all_attributes, parse_attribute_header, AttrType,
    ATTR_FLAG_COMPRESSED, ATTR_FLAG_ENCRYPTED,
)
from ddtriage.ntfs.tree import build_tree, FileRecord


class TestCorruptedMftRecords:
    def test_truncated_record(self):
        """A record shorter than record_size should return None."""
        data = b'FILE' + b'\x00' * 100
        record = parse_mft_record(data, 0, record_size=1024)
        assert record is None

    def test_all_zeros_record(self):
        """All-zero record should return None (empty slot)."""
        record = parse_mft_record(b'\x00' * 1024, 0)
        assert record is None

    def test_baad_signature(self):
        """BAAD signature indicates incomplete write."""
        data = bytearray(1024)
        data[0:4] = b'BAAD'
        # Need valid USA fields for fixup to not crash
        struct.pack_into('<H', data, 0x04, 0x30)  # usa offset
        struct.pack_into('<H', data, 0x06, 3)      # usa count
        record = parse_mft_record(bytes(data), 0)
        assert record is not None
        assert record.parse_error is not None
        assert "BAAD" in record.parse_error or "Fixup" in record.parse_error

    def test_garbage_attributes(self):
        """Record with valid header but garbage attribute area."""
        data = bytearray(1024)
        data[0:4] = b'FILE'
        struct.pack_into('<H', data, 0x04, 0x30)
        struct.pack_into('<H', data, 0x06, 3)
        struct.pack_into('<H', data, 0x14, 0x38)  # first attr offset
        struct.pack_into('<H', data, 0x16, 0x01)  # in_use
        struct.pack_into('<I', data, 0x18, 1024)
        struct.pack_into('<I', data, 0x1C, 1024)
        # Fill attribute area with random-ish bytes
        for i in range(0x38, 1024):
            data[i] = (i * 7 + 13) & 0xFF

        # Set up valid fixup
        expected = 0xBEEF
        struct.pack_into('<H', data, 0x30, expected)
        for i in range(1, 3):
            sector_end = i * 512 - 2
            original = struct.unpack_from('<H', data, sector_end)[0]
            struct.pack_into('<H', data, 0x30 + i * 2, original)
            struct.pack_into('<H', data, sector_end, expected)

        record = parse_mft_record(bytes(data), 0)
        # Should not crash — might have parse_error or just garbled attributes
        assert record is not None

    def test_zero_length_attribute_no_infinite_loop(self):
        """An attribute with length 0 should not cause an infinite loop."""
        data = bytearray(256)
        # Fake attribute at offset 0: type=0x10, length=0
        struct.pack_into('<I', data, 0, AttrType.STANDARD_INFORMATION)
        struct.pack_into('<I', data, 4, 0)  # zero length

        attrs = parse_all_attributes(bytes(data), 0)
        # Should return empty or minimal results, not hang
        assert isinstance(attrs, list)

    def test_attribute_length_exceeds_record(self):
        """Attribute claiming to be larger than remaining data."""
        data = bytearray(64)
        struct.pack_into('<I', data, 0, AttrType.FILE_NAME)
        struct.pack_into('<I', data, 4, 99999)  # absurd length
        data[8] = 0  # resident

        header = parse_attribute_header(bytes(data), 0)
        assert header is None  # should reject


class TestAttributeFlags:
    def test_compressed_flag(self):
        data = bytearray(32)
        struct.pack_into('<I', data, 0, AttrType.DATA)
        struct.pack_into('<I', data, 4, 24)  # length
        data[8] = 0  # resident
        struct.pack_into('<H', data, 0x0C, ATTR_FLAG_COMPRESSED)

        header = parse_attribute_header(bytes(data), 0)
        assert header is not None
        assert header.is_compressed is True
        assert header.is_encrypted is False

    def test_encrypted_flag(self):
        data = bytearray(32)
        struct.pack_into('<I', data, 0, AttrType.DATA)
        struct.pack_into('<I', data, 4, 24)
        data[8] = 0
        struct.pack_into('<H', data, 0x0C, ATTR_FLAG_ENCRYPTED)

        header = parse_attribute_header(bytes(data), 0)
        assert header.is_encrypted is True
        assert header.is_compressed is False


class TestIterMftRecordsHardened:
    def test_many_empty_slots_continue(self):
        """Parser should tolerate stretches of empty slots within MFT."""
        # Build: 2 valid records, 10 empty, 2 more valid, then end
        record_size = 1024

        def _make_minimal_record(index):
            rec = bytearray(record_size)
            rec[0:4] = b'FILE'
            struct.pack_into('<H', rec, 0x04, 0x30)
            struct.pack_into('<H', rec, 0x06, 3)
            struct.pack_into('<H', rec, 0x14, 0x38)  # first attr offset
            struct.pack_into('<H', rec, 0x16, 0x01)  # flags: in_use
            struct.pack_into('<I', rec, 0x18, record_size)
            struct.pack_into('<I', rec, 0x1C, record_size)
            # End marker at first attr
            struct.pack_into('<I', rec, 0x38, 0xFFFFFFFF)
            # Fixup
            expected = 0xBEEF
            struct.pack_into('<H', rec, 0x30, expected)
            for i in range(1, 3):
                se = i * 512 - 2
                orig = struct.unpack_from('<H', rec, se)[0]
                struct.pack_into('<H', rec, 0x30 + i * 2, orig)
                struct.pack_into('<H', rec, se, expected)
            return bytes(rec)

        image = bytearray()
        # Offset = 0
        for i in range(2):
            image.extend(_make_minimal_record(i))
        for i in range(10):
            image.extend(b'\x00' * record_size)
        for i in range(2):
            image.extend(_make_minimal_record(i + 12))
        image.extend(b'\x00' * record_size * 50)  # lots of empty at end

        records = iter_mft_records(bytes(image), 0, record_size)
        # Should get at least 4 records (2 + 2), maybe more depending on threshold
        assert len(records) >= 4

    def test_negative_offset_rejected(self):
        """Negative MFT offset should produce empty results."""
        records = iter_mft_records(b'\x00' * 4096, -100, 1024)
        assert records == []


class TestTreeWithDeletedAndFlags:
    def test_compressed_flag_in_tree(self):
        """FileRecord should carry is_compressed from $DATA attribute."""
        from ddtriage.ntfs.attributes import (
            ParsedAttribute, AttributeHeader, ResidentData,
            FileName, FileNamespace,
        )
        from ddtriage.ntfs.mft_parser import MftRecord as MR

        # Build a record with a compressed $DATA attribute
        fn = FileName(
            parent_reference=5, parent_mft_index=5, parent_sequence=1,
            created=None, modified=None, mft_modified=None, accessed=None,
            allocated_size=100, real_size=100, flags=0,
            name_length=4, namespace=FileNamespace.WIN32_AND_DOS, name="test",
        )
        fn_attr = ParsedAttribute(
            header=AttributeHeader(
                type=AttrType.FILE_NAME, length=0, non_resident=False,
                name_length=0, name_offset=0, flags=0, attribute_id=0, name="",
            ),
            resident=ResidentData(data_length=0, data_offset=0, data=b''),
            file_name=fn,
        )
        data_attr = ParsedAttribute(
            header=AttributeHeader(
                type=AttrType.DATA, length=0, non_resident=False,
                name_length=0, name_offset=0,
                flags=ATTR_FLAG_COMPRESSED,
                attribute_id=1, name="",
            ),
            resident=ResidentData(data_length=5, data_offset=0, data=b'hello'),
        )

        root_rec = MR(
            index=5, signature=b'FILE', sequence_number=1,
            hard_link_count=1, flags=0x03, used_size=1024, allocated_size=1024,
            base_record_reference=0, base_record_index=0,
            attributes=[fn_attr],
        )
        file_rec = MR(
            index=16, signature=b'FILE', sequence_number=1,
            hard_link_count=1, flags=0x01, used_size=1024, allocated_size=1024,
            base_record_reference=0, base_record_index=0,
            attributes=[fn_attr, data_attr],
        )

        tree = build_tree([root_rec, file_rec])
        assert len(tree.root.children) == 1
        assert tree.root.children[0].is_compressed is True
