"""Tests for MFT record parsing including fixup and attribute extraction."""

import struct
import pytest

from ddtriage.ntfs.mft_parser import parse_mft_record, apply_fixup, MftRecord
from ddtriage.ntfs.attributes import AttrType


def _build_mft_record(
    flags: int = 0x01,  # in_use
    sequence: int = 1,
    hard_links: int = 1,
    base_ref: int = 0,
    attrs: list[bytes] | None = None,
) -> bytes:
    """Build a minimal 1024-byte MFT record with proper fixup.

    The record has 2 sectors (512 bytes each), so the update sequence array
    has 3 entries: 1 expected value + 2 original values.
    """
    record = bytearray(1024)
    record_size = 1024

    # Signature
    record[0:4] = b'FILE'

    # Update sequence array offset and count
    usa_offset = 0x30  # after the standard header
    usa_count = 3       # 1 expected + 2 sector fixups
    struct.pack_into('<H', record, 0x04, usa_offset)
    struct.pack_into('<H', record, 0x06, usa_count)

    # Sequence number
    struct.pack_into('<H', record, 0x10, sequence)

    # Hard link count
    struct.pack_into('<H', record, 0x12, hard_links)

    # First attribute offset (after header + USA)
    first_attr = usa_offset + usa_count * 2  # 0x30 + 6 = 0x36
    # Align to 8 bytes
    first_attr = (first_attr + 7) & ~7  # 0x38
    struct.pack_into('<H', record, 0x14, first_attr)

    # Flags
    struct.pack_into('<H', record, 0x16, flags)

    # Used size / allocated size
    struct.pack_into('<I', record, 0x18, record_size)
    struct.pack_into('<I', record, 0x1C, record_size)

    # Base record reference
    struct.pack_into('<Q', record, 0x20, base_ref)

    # Write attributes
    attr_offset = first_attr
    if attrs:
        for attr_bytes in attrs:
            record[attr_offset:attr_offset + len(attr_bytes)] = attr_bytes
            attr_offset += len(attr_bytes)

    # End marker
    struct.pack_into('<I', record, attr_offset, 0xFFFFFFFF)

    # Now apply the update sequence:
    # Expected value = 0xBEEF
    expected = 0xBEEF
    struct.pack_into('<H', record, usa_offset, expected)

    # Save original last 2 bytes of each sector, replace with expected
    for i in range(1, usa_count):
        sector_end = i * 512 - 2
        original = struct.unpack_from('<H', record, sector_end)[0]
        struct.pack_into('<H', record, usa_offset + i * 2, original)
        struct.pack_into('<H', record, sector_end, expected)

    return bytes(record)


def _build_filename_attr(
    name: str = "test.txt",
    parent_mft: int = 5,
    parent_seq: int = 1,
    namespace: int = 3,  # WIN32_AND_DOS
    file_size: int = 1234,
) -> bytes:
    """Build a resident $FILE_NAME attribute."""
    name_bytes = name.encode('utf-16-le')
    name_chars = len(name)

    # $FILE_NAME resident data: 66 bytes header + name
    fn_data = bytearray(66 + len(name_bytes))

    # Parent reference (6 bytes index + 2 bytes sequence)
    parent_ref = parent_mft | (parent_seq << 48)
    struct.pack_into('<Q', fn_data, 0, parent_ref)

    # Timestamps (set to 0 for simplicity)
    # created=8, modified=16, mft_modified=24, accessed=32

    # Allocated size
    struct.pack_into('<Q', fn_data, 40, file_size)
    # Real size
    struct.pack_into('<Q', fn_data, 48, file_size)
    # Flags
    struct.pack_into('<I', fn_data, 56, 0)

    # Name length and namespace
    fn_data[64] = name_chars
    fn_data[65] = namespace

    # Name
    fn_data[66:66 + len(name_bytes)] = name_bytes

    # Now wrap in an attribute header (resident)
    data_offset = 0x18  # standard resident header size
    # Check if we need name — no attribute name here
    attr_length = data_offset + len(fn_data)
    # Align to 8 bytes
    attr_length = (attr_length + 7) & ~7

    attr = bytearray(attr_length)
    # Type: $FILE_NAME
    struct.pack_into('<I', attr, 0x00, AttrType.FILE_NAME)
    # Length
    struct.pack_into('<I', attr, 0x04, attr_length)
    # Non-resident flag: 0 (resident)
    attr[0x08] = 0
    # Name length: 0
    attr[0x09] = 0
    # Name offset
    struct.pack_into('<H', attr, 0x0A, 0)
    # Flags
    struct.pack_into('<H', attr, 0x0C, 0)
    # Attribute ID
    struct.pack_into('<H', attr, 0x0E, 1)

    # Resident: data length and data offset
    struct.pack_into('<I', attr, 0x10, len(fn_data))
    struct.pack_into('<H', attr, 0x14, data_offset)

    # Data
    attr[data_offset:data_offset + len(fn_data)] = fn_data

    return bytes(attr)


def _build_data_attr_resident(data: bytes) -> bytes:
    """Build a resident $DATA attribute with the given content."""
    data_offset = 0x18
    attr_length = data_offset + len(data)
    attr_length = (attr_length + 7) & ~7

    attr = bytearray(attr_length)
    struct.pack_into('<I', attr, 0x00, AttrType.DATA)
    struct.pack_into('<I', attr, 0x04, attr_length)
    attr[0x08] = 0  # resident
    attr[0x09] = 0
    struct.pack_into('<H', attr, 0x0A, 0)
    struct.pack_into('<H', attr, 0x0C, 0)
    struct.pack_into('<H', attr, 0x0E, 2)
    struct.pack_into('<I', attr, 0x10, len(data))
    struct.pack_into('<H', attr, 0x14, data_offset)
    attr[data_offset:data_offset + len(data)] = data

    return bytes(attr)


class TestFixup:
    def test_fixup_applied(self):
        raw = bytearray(_build_mft_record())
        # The record has fixup applied during construction.
        # apply_fixup should restore original values.
        assert apply_fixup(raw, 1024) is True

    def test_fixup_mismatch(self):
        raw = bytearray(_build_mft_record())
        # Corrupt the expected value at a sector boundary
        raw[510] = 0x00
        raw[511] = 0x00
        assert apply_fixup(raw, 1024) is False


class TestMftRecord:
    def test_parse_basic_record(self):
        data = _build_mft_record(flags=0x01, sequence=5)
        record = parse_mft_record(data, record_index=42)

        assert record is not None
        assert record.index == 42
        assert record.signature == b'FILE'
        assert record.sequence_number == 5
        assert record.in_use is True
        assert record.is_directory is False
        assert record.is_base_record is True
        assert record.parse_error is None

    def test_parse_directory_record(self):
        data = _build_mft_record(flags=0x03)  # in_use + directory
        record = parse_mft_record(data, record_index=5)

        assert record.in_use is True
        assert record.is_directory is True

    def test_parse_with_filename(self):
        fn_attr = _build_filename_attr(name="hello.txt", parent_mft=5, file_size=42)
        data = _build_mft_record(attrs=[fn_attr])
        record = parse_mft_record(data, record_index=16)

        assert record is not None
        assert record.parse_error is None
        fn_attrs = record.get_attributes(AttrType.FILE_NAME)
        assert len(fn_attrs) == 1
        assert fn_attrs[0].file_name is not None
        assert fn_attrs[0].file_name.name == "hello.txt"
        assert fn_attrs[0].file_name.parent_mft_index == 5

    def test_parse_with_resident_data(self):
        content = b"Small file content"
        fn_attr = _build_filename_attr(name="tiny.txt", file_size=len(content))
        data_attr = _build_data_attr_resident(content)
        data = _build_mft_record(attrs=[fn_attr, data_attr])
        record = parse_mft_record(data, record_index=20)

        assert record is not None
        data_attrs = record.get_attributes(AttrType.DATA)
        assert len(data_attrs) == 1
        assert data_attrs[0].resident is not None
        assert data_attrs[0].resident.data == content

    def test_empty_record(self):
        data = b'\x00' * 1024
        record = parse_mft_record(data, record_index=0)
        assert record is None

    def test_bad_signature(self):
        data = bytearray(1024)
        data[0:4] = b'JUNK'
        record = parse_mft_record(bytes(data), record_index=0)
        assert record is not None
        assert record.parse_error is not None
        assert "Bad signature" in record.parse_error

    def test_extension_record(self):
        base_ref = 16 | (1 << 48)  # MFT record 16, sequence 1
        data = _build_mft_record(base_ref=base_ref)
        record = parse_mft_record(data, record_index=100)
        assert record.is_base_record is False
        assert record.base_record_index == 16

    def test_unicode_filename(self):
        fn_attr = _build_filename_attr(name="résumé.pdf")
        data = _build_mft_record(attrs=[fn_attr])
        record = parse_mft_record(data, record_index=30)
        fn_attrs = record.get_attributes(AttrType.FILE_NAME)
        assert fn_attrs[0].file_name.name == "résumé.pdf"
