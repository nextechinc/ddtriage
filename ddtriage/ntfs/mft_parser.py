"""Parse NTFS MFT (Master File Table) entries."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field

from .attributes import (
    ParsedAttribute, AttrType, parse_all_attributes,
    parse_attribute_list, NonResidentData,
)

log = logging.getLogger(__name__)

MFT_SIGNATURE = b'FILE'
BAAD_SIGNATURE = b'BAAD'


@dataclass
class MftRecord:
    """A parsed MFT record with its attributes."""
    index: int                              # MFT record number
    signature: bytes                        # 'FILE' or 'BAAD'
    sequence_number: int
    hard_link_count: int
    flags: int                              # 1=in_use, 2=directory
    used_size: int
    allocated_size: int
    base_record_reference: int              # 0 for base records, non-zero for extensions
    base_record_index: int                  # lower 6 bytes of base_record_reference
    attributes: list[ParsedAttribute] = field(default_factory=list)
    parse_error: str | None = None          # set if record was partially parsed

    @property
    def in_use(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def is_base_record(self) -> bool:
        return self.base_record_index == 0

    def get_attributes(self, attr_type: int) -> list[ParsedAttribute]:
        """Return all attributes of the given type."""
        return [a for a in self.attributes if a.header.type == attr_type]


def apply_fixup(data: bytearray, record_size: int) -> bool:
    """Apply the update sequence (fixup) array to an MFT record.

    The fixup mechanism protects against torn writes. The last 2 bytes of each
    sector are replaced with a sequence number during write; we restore the
    original values here.

    Returns True if fixup was applied successfully, False on error.
    """
    if len(data) < 48:
        return False

    usa_offset = struct.unpack_from('<H', data, 0x04)[0]
    usa_count = struct.unpack_from('<H', data, 0x06)[0]

    if usa_count < 2 or usa_offset + usa_count * 2 > len(data):
        return False

    # First entry is the expected value at each sector boundary
    expected = struct.unpack_from('<H', data, usa_offset)[0]
    sector_size = 512

    for i in range(1, usa_count):
        sector_end = i * sector_size - 2
        if sector_end + 2 > len(data):
            break

        actual = struct.unpack_from('<H', data, sector_end)[0]
        if actual != expected:
            log.debug("Fixup mismatch at sector %d: expected 0x%04X, got 0x%04X",
                      i, expected, actual)
            return False

        # Replace with original value from the update sequence array
        original = data[usa_offset + i * 2:usa_offset + i * 2 + 2]
        data[sector_end:sector_end + 2] = original

    return True


def parse_mft_record(data: bytes, record_index: int, record_size: int = 1024) -> MftRecord | None:
    """Parse a single MFT record from raw bytes.

    Args:
        data: Raw bytes of exactly one MFT record.
        record_index: The MFT record number (for tracking).
        record_size: Expected record size in bytes (typically 1024).

    Returns MftRecord on success, None if the record is empty/unreadable.
    """
    if len(data) < record_size:
        return None

    record_data = bytearray(data[:record_size])

    # Check signature
    sig = bytes(record_data[0:4])
    if sig == b'\x00\x00\x00\x00':
        return None  # empty/unused slot
    if sig not in (MFT_SIGNATURE, BAAD_SIGNATURE):
        return MftRecord(
            index=record_index, signature=sig,
            sequence_number=0, hard_link_count=0, flags=0,
            used_size=0, allocated_size=record_size,
            base_record_reference=0, base_record_index=0,
            parse_error=f"Bad signature: {sig!r}",
        )

    # Apply fixup before parsing anything else
    fixup_ok = apply_fixup(record_data, record_size)

    sequence_number = struct.unpack_from('<H', record_data, 0x10)[0]
    hard_link_count = struct.unpack_from('<H', record_data, 0x12)[0]
    first_attr_offset = struct.unpack_from('<H', record_data, 0x14)[0]
    flags = struct.unpack_from('<H', record_data, 0x16)[0]
    used_size = struct.unpack_from('<I', record_data, 0x18)[0]
    allocated_size = struct.unpack_from('<I', record_data, 0x1C)[0]
    base_ref = struct.unpack_from('<Q', record_data, 0x20)[0]
    base_record_index = base_ref & 0x0000FFFFFFFFFFFF

    record = MftRecord(
        index=record_index,
        signature=sig,
        sequence_number=sequence_number,
        hard_link_count=hard_link_count,
        flags=flags,
        used_size=used_size,
        allocated_size=allocated_size,
        base_record_reference=base_ref,
        base_record_index=base_record_index,
    )

    if not fixup_ok:
        record.parse_error = "Fixup failed"
        return record

    if sig == BAAD_SIGNATURE:
        record.parse_error = "BAAD signature (incomplete write)"
        return record

    # Parse attributes
    try:
        record.attributes = parse_all_attributes(bytes(record_data), first_attr_offset)
    except Exception as e:
        record.parse_error = f"Attribute parse error: {e}"

    return record


@dataclass
class MftParseStats:
    """Statistics from an MFT parsing run."""
    total_scanned: int = 0
    valid_records: int = 0
    empty_slots: int = 0
    damaged_records: int = 0    # records with parse_error set
    in_use_records: int = 0
    directory_count: int = 0


def iter_mft_records(
    image_data: bytes,
    mft_offset: int,
    record_size: int = 1024,
    max_records: int | None = None,
    progress_callback=None,
) -> list[MftRecord]:
    """Parse all MFT records from image data.

    Args:
        image_data: Raw image bytes (or memory-mapped file).
        mft_offset: Absolute byte offset of the MFT in the image.
        record_size: MFT record size in bytes.
        max_records: Stop after this many records (None = parse until data runs out or
                     we see too many consecutive empty slots).
        progress_callback: Optional callable(index, record_or_none) called per slot.

    Returns list of successfully parsed MftRecord objects.
    """
    records: list[MftRecord] = []
    stats = MftParseStats()
    consecutive_empty = 0
    index = 0
    # More generous empty-slot threshold for disks with gaps in the image
    empty_threshold = 32

    while True:
        if max_records is not None and index >= max_records:
            break

        offset = mft_offset + index * record_size
        if offset < 0 or offset + record_size > len(image_data):
            break

        chunk = image_data[offset:offset + record_size]

        try:
            record = parse_mft_record(chunk, index, record_size)
        except Exception as e:
            log.warning("Exception parsing MFT record %d: %s", index, e)
            record = MftRecord(
                index=index, signature=b'\x00\x00\x00\x00',
                sequence_number=0, hard_link_count=0, flags=0,
                used_size=0, allocated_size=record_size,
                base_record_reference=0, base_record_index=0,
                parse_error=f"Parse exception: {e}",
            )

        if progress_callback is not None:
            progress_callback(index, record)

        if record is None:
            consecutive_empty += 1
            stats.empty_slots += 1
            if consecutive_empty > empty_threshold and index > 16:
                break
        else:
            consecutive_empty = 0
            records.append(record)
            stats.valid_records += 1
            if record.parse_error:
                stats.damaged_records += 1
            if record.in_use:
                stats.in_use_records += 1
            if record.is_directory:
                stats.directory_count += 1

        index += 1

    stats.total_scanned = index
    log.info(
        "MFT parse complete: %d scanned, %d valid (%d in-use, %d dirs), "
        "%d damaged, %d empty",
        stats.total_scanned, stats.valid_records, stats.in_use_records,
        stats.directory_count, stats.damaged_records, stats.empty_slots,
    )
    return records
