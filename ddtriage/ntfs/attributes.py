"""Parse NTFS MFT attribute headers and key attribute types."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import IntEnum

from .data_runs import decode_data_runs

log = logging.getLogger(__name__)

# Attribute flags
ATTR_FLAG_COMPRESSED = 0x0001
ATTR_FLAG_ENCRYPTED = 0x4000
ATTR_FLAG_SPARSE = 0x8000


class AttrType(IntEnum):
    STANDARD_INFORMATION = 0x10
    ATTRIBUTE_LIST = 0x20
    FILE_NAME = 0x30
    OBJECT_ID = 0x40
    SECURITY_DESCRIPTOR = 0x50
    VOLUME_NAME = 0x60
    VOLUME_INFORMATION = 0x70
    DATA = 0x80
    INDEX_ROOT = 0x90
    INDEX_ALLOCATION = 0xA0
    BITMAP = 0xB0
    REPARSE_POINT = 0xC0
    END_MARKER = 0xFFFFFFFF


class FileNamespace(IntEnum):
    POSIX = 0
    WIN32 = 1
    DOS = 2
    WIN32_AND_DOS = 3


# NTFS epoch: 1601-01-01 00:00:00 UTC
_NTFS_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def ntfs_timestamp_to_datetime(ts: int) -> datetime | None:
    """Convert NTFS 64-bit timestamp (100-nanosecond intervals since 1601-01-01) to datetime."""
    if ts == 0:
        return None
    try:
        return _NTFS_EPOCH + timedelta(microseconds=ts // 10)
    except (OverflowError, OSError):
        return None


@dataclass
class AttributeHeader:
    """Common attribute header present in all MFT attributes."""
    type: int
    length: int
    non_resident: bool
    name_length: int
    name_offset: int
    flags: int
    attribute_id: int
    name: str  # decoded attribute name (empty string if unnamed)

    # Offset of this attribute within the MFT record (for debugging)
    record_offset: int = 0

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & ATTR_FLAG_COMPRESSED)

    @property
    def is_encrypted(self) -> bool:
        return bool(self.flags & ATTR_FLAG_ENCRYPTED)

    @property
    def is_sparse(self) -> bool:
        return bool(self.flags & ATTR_FLAG_SPARSE)


@dataclass
class ResidentData:
    """Data from a resident attribute."""
    data_length: int
    data_offset: int  # relative to attribute start
    data: bytes


@dataclass
class NonResidentData:
    """Data from a non-resident attribute."""
    start_vcn: int
    end_vcn: int
    data_runs_offset: int  # relative to attribute start
    allocated_size: int
    real_size: int
    initialized_size: int
    data_runs: list[tuple[int | None, int]]  # decoded (lcn, cluster_count)


@dataclass
class StandardInformation:
    """Parsed $STANDARD_INFORMATION attribute."""
    created: datetime | None
    modified: datetime | None
    mft_modified: datetime | None
    accessed: datetime | None
    flags: int


@dataclass
class FileName:
    """Parsed $FILE_NAME attribute."""
    parent_reference: int       # raw 8-byte reference
    parent_mft_index: int       # lower 6 bytes
    parent_sequence: int        # upper 2 bytes
    created: datetime | None
    modified: datetime | None
    mft_modified: datetime | None
    accessed: datetime | None
    allocated_size: int
    real_size: int
    flags: int
    name_length: int
    namespace: int
    name: str


@dataclass
class AttributeListEntry:
    """One entry from an $ATTRIBUTE_LIST attribute."""
    attr_type: int
    record_length: int
    name_length: int
    name_offset: int
    start_vcn: int
    mft_reference: int      # raw 8-byte reference
    mft_record_number: int  # lower 6 bytes
    mft_sequence: int       # upper 2 bytes
    attribute_id: int
    name: str


@dataclass
class ParsedAttribute:
    """A fully parsed attribute with header and type-specific data."""
    header: AttributeHeader
    resident: ResidentData | None = None
    non_resident: NonResidentData | None = None
    # Type-specific parsed data (filled for known types)
    standard_info: StandardInformation | None = None
    file_name: FileName | None = None
    attribute_list_entries: list[AttributeListEntry] | None = None


def parse_attribute_header(data: bytes, offset: int) -> AttributeHeader | None:
    """Parse the common attribute header at the given offset.

    Returns None if we hit the end marker or data is insufficient.
    """
    if offset < 0 or offset + 16 > len(data):
        return None

    attr_type = struct.unpack_from('<I', data, offset)[0]
    if attr_type == AttrType.END_MARKER or attr_type == 0:
        return None

    attr_length = struct.unpack_from('<I', data, offset + 4)[0]
    if attr_length < 16 or attr_length > len(data) - offset:
        return None
    # Guard against attr_length not being aligned (corrupt data)
    if attr_length % 8 != 0:
        log.debug("Attribute at 0x%X has unaligned length %d", offset, attr_length)
        # Don't reject — some tools produce unaligned attrs; round up for safety
        pass

    non_resident = data[offset + 8] != 0
    name_length = data[offset + 9]
    name_offset = struct.unpack_from('<H', data, offset + 0x0A)[0]
    flags = struct.unpack_from('<H', data, offset + 0x0C)[0]
    attribute_id = struct.unpack_from('<H', data, offset + 0x0E)[0]

    # Decode attribute name if present
    name = ''
    if name_length > 0 and name_offset > 0:
        name_start = offset + name_offset
        name_end = name_start + name_length * 2  # UTF-16LE
        if name_end <= len(data):
            try:
                name = data[name_start:name_end].decode('utf-16-le')
            except UnicodeDecodeError:
                pass

    return AttributeHeader(
        type=attr_type,
        length=attr_length,
        non_resident=non_resident,
        name_length=name_length,
        name_offset=name_offset,
        flags=flags,
        attribute_id=attribute_id,
        name=name,
        record_offset=offset,
    )


def parse_resident_data(data: bytes, attr_offset: int) -> ResidentData | None:
    """Parse resident attribute data fields."""
    if attr_offset + 0x18 > len(data):
        return None

    data_length = struct.unpack_from('<I', data, attr_offset + 0x10)[0]
    data_offset = struct.unpack_from('<H', data, attr_offset + 0x14)[0]

    abs_start = attr_offset + data_offset
    abs_end = abs_start + data_length
    if abs_end > len(data):
        return None

    return ResidentData(
        data_length=data_length,
        data_offset=data_offset,
        data=data[abs_start:abs_end],
    )


def parse_non_resident_data(data: bytes, attr_offset: int, attr_length: int) -> NonResidentData | None:
    """Parse non-resident attribute data fields and decode data runs."""
    if attr_offset + 0x40 > len(data):
        return None

    start_vcn = struct.unpack_from('<Q', data, attr_offset + 0x10)[0]
    end_vcn = struct.unpack_from('<Q', data, attr_offset + 0x18)[0]
    runs_offset = struct.unpack_from('<H', data, attr_offset + 0x20)[0]
    allocated_size = struct.unpack_from('<Q', data, attr_offset + 0x28)[0]
    real_size = struct.unpack_from('<Q', data, attr_offset + 0x30)[0]
    initialized_size = struct.unpack_from('<Q', data, attr_offset + 0x38)[0]

    runs_start = attr_offset + runs_offset
    runs_end = attr_offset + attr_length
    if runs_start >= len(data):
        runs = []
    else:
        runs = decode_data_runs(data, runs_start)

    return NonResidentData(
        start_vcn=start_vcn,
        end_vcn=end_vcn,
        data_runs_offset=runs_offset,
        allocated_size=allocated_size,
        real_size=real_size,
        initialized_size=initialized_size,
        data_runs=runs,
    )


def parse_standard_information(resident: ResidentData) -> StandardInformation | None:
    """Parse $STANDARD_INFORMATION from resident data."""
    d = resident.data
    if len(d) < 48:
        return None

    return StandardInformation(
        created=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 0)[0]),
        modified=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 8)[0]),
        mft_modified=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 16)[0]),
        accessed=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 24)[0]),
        flags=struct.unpack_from('<I', d, 32)[0],
    )


def parse_file_name(resident: ResidentData) -> FileName | None:
    """Parse $FILE_NAME from resident data."""
    d = resident.data
    if len(d) < 66:
        return None

    parent_ref = struct.unpack_from('<Q', d, 0)[0]
    parent_mft_index = parent_ref & 0x0000FFFFFFFFFFFF
    parent_sequence = (parent_ref >> 48) & 0xFFFF

    name_length = d[64]
    namespace = d[65]

    name_start = 66
    name_end = name_start + name_length * 2
    if name_end > len(d):
        return None

    try:
        name = d[name_start:name_end].decode('utf-16-le')
    except UnicodeDecodeError:
        name = d[name_start:name_end].decode('utf-16-le', errors='replace')

    return FileName(
        parent_reference=parent_ref,
        parent_mft_index=parent_mft_index,
        parent_sequence=parent_sequence,
        created=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 8)[0]),
        modified=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 16)[0]),
        mft_modified=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 24)[0]),
        accessed=ntfs_timestamp_to_datetime(struct.unpack_from('<Q', d, 32)[0]),
        allocated_size=struct.unpack_from('<Q', d, 40)[0],
        real_size=struct.unpack_from('<Q', d, 48)[0],
        flags=struct.unpack_from('<I', d, 56)[0],
        name_length=name_length,
        namespace=namespace,
        name=name,
    )


def parse_attribute_list(data: bytes) -> list[AttributeListEntry]:
    """Parse $ATTRIBUTE_LIST entries from raw attribute data (resident or reassembled)."""
    entries: list[AttributeListEntry] = []
    pos = 0

    while pos + 26 <= len(data):
        attr_type = struct.unpack_from('<I', data, pos)[0]
        record_length = struct.unpack_from('<H', data, pos + 4)[0]
        if record_length < 26:
            break

        name_length = data[pos + 6]
        name_offset = data[pos + 7]
        start_vcn = struct.unpack_from('<Q', data, pos + 8)[0]
        mft_ref = struct.unpack_from('<Q', data, pos + 16)[0]
        mft_record_number = mft_ref & 0x0000FFFFFFFFFFFF
        mft_sequence = (mft_ref >> 48) & 0xFFFF
        attribute_id = struct.unpack_from('<H', data, pos + 24)[0]

        name = ''
        if name_length > 0:
            ns = pos + name_offset
            ne = ns + name_length * 2
            if ne <= len(data):
                try:
                    name = data[ns:ne].decode('utf-16-le')
                except UnicodeDecodeError:
                    pass

        entries.append(AttributeListEntry(
            attr_type=attr_type,
            record_length=record_length,
            name_length=name_length,
            name_offset=name_offset,
            start_vcn=start_vcn,
            mft_reference=mft_ref,
            mft_record_number=mft_record_number,
            mft_sequence=mft_sequence,
            attribute_id=attribute_id,
            name=name,
        ))

        pos += record_length

    return entries


def parse_attribute(data: bytes, offset: int) -> ParsedAttribute | None:
    """Parse a single attribute at the given offset within an MFT record.

    Returns None if we hit the end marker or the data is too short/corrupt.
    """
    header = parse_attribute_header(data, offset)
    if header is None:
        return None

    attr = ParsedAttribute(header=header)

    if header.non_resident:
        attr.non_resident = parse_non_resident_data(data, offset, header.length)
    else:
        attr.resident = parse_resident_data(data, offset)

    # Parse type-specific data for known types
    if header.type == AttrType.STANDARD_INFORMATION and attr.resident:
        attr.standard_info = parse_standard_information(attr.resident)
    elif header.type == AttrType.FILE_NAME and attr.resident:
        attr.file_name = parse_file_name(attr.resident)
    elif header.type == AttrType.ATTRIBUTE_LIST and attr.resident:
        attr.attribute_list_entries = parse_attribute_list(attr.resident.data)

    return attr


def parse_all_attributes(data: bytes, first_attr_offset: int) -> list[ParsedAttribute]:
    """Parse all attributes in an MFT record starting at the given offset.

    Hardened: per-attribute try/except so one corrupted attribute doesn't
    lose the entire record. Also guards against infinite loops from zero-length
    or backwards-pointing attributes.
    """
    attrs: list[ParsedAttribute] = []
    offset = first_attr_offset
    max_attrs = 64  # safety limit — real records rarely exceed ~20 attributes

    if first_attr_offset < 0 or first_attr_offset >= len(data):
        return attrs

    for _ in range(max_attrs):
        if offset >= len(data) - 8:
            break

        try:
            attr = parse_attribute(data, offset)
        except Exception as e:
            log.debug("Exception parsing attribute at offset 0x%X: %s", offset, e)
            break

        if attr is None:
            break

        # Guard against zero-length attributes (would infinite-loop)
        if attr.header.length <= 0:
            log.debug("Zero/negative attribute length at 0x%X, stopping", offset)
            break

        attrs.append(attr)
        offset += attr.header.length

    return attrs
