"""Tests for BitLocker detection and integration."""

import struct
import pytest

from ddtriage.ntfs.boot_sector import (
    detect_bitlocker, parse_boot_sector, BitLockerDetected,
    BITLOCKER_SIGNATURE,
)


def _build_bitlocker_sector() -> bytes:
    """Build a minimal 512-byte BitLocker boot sector."""
    data = bytearray(512)
    data[0:3] = b'\xEB\x58\x90'  # jump instruction
    data[0x03:0x0B] = BITLOCKER_SIGNATURE  # -FVE-FS-
    return bytes(data)


def _build_ntfs_sector() -> bytes:
    """Build a minimal valid NTFS boot sector."""
    data = bytearray(512)
    data[0:3] = b'\xEB\x52\x90'
    data[0x03:0x0B] = b'NTFS    '
    struct.pack_into('<H', data, 0x0B, 512)  # bytes per sector
    data[0x0D] = 8  # sectors per cluster
    struct.pack_into('<Q', data, 0x28, 1000)  # total sectors
    struct.pack_into('<Q', data, 0x30, 100)   # MFT LCN
    struct.pack_into('<Q', data, 0x38, 2)     # MFT mirror LCN
    struct.pack_into('<b', data, 0x40, -10)   # clusters per MFT record
    struct.pack_into('<b', data, 0x44, -12)   # clusters per index block
    return bytes(data)


class TestDetectBitLocker:
    def test_detect_bitlocker_positive(self):
        data = _build_bitlocker_sector()
        assert detect_bitlocker(data) is True

    def test_detect_bitlocker_negative_ntfs(self):
        data = _build_ntfs_sector()
        assert detect_bitlocker(data) is False

    def test_detect_bitlocker_negative_zeros(self):
        data = b'\x00' * 512
        assert detect_bitlocker(data) is False

    def test_detect_bitlocker_too_short(self):
        assert detect_bitlocker(b'\x00' * 5) is False

    def test_detect_bitlocker_with_offset(self):
        prefix = b'\x00' * 1024
        data = prefix + _build_bitlocker_sector()
        assert detect_bitlocker(data, offset=1024) is True
        assert detect_bitlocker(data, offset=0) is False


class TestParseBootSectorBitLocker:
    def test_raises_bitlocker_detected(self):
        data = _build_bitlocker_sector()
        with pytest.raises(BitLockerDetected) as exc_info:
            parse_boot_sector(data)
        assert "BitLocker" in str(exc_info.value)
        assert "--recovery-key" in str(exc_info.value)

    def test_ntfs_still_works(self):
        data = _build_ntfs_sector()
        bs = parse_boot_sector(data)
        assert bs.oem_id == 'NTFS'

    def test_bitlocker_at_offset(self):
        prefix = b'\x00' * 512
        data = prefix + _build_bitlocker_sector()
        with pytest.raises(BitLockerDetected):
            parse_boot_sector(data, offset=512)


class TestSessionStateBitLocker:
    def test_state_has_bitlocker_fields(self):
        from ddtriage.bootstrap import SessionState
        state = SessionState()
        assert state.bitlocker is False
        assert state.bitlocker_partition == ""

    def test_state_roundtrip(self):
        import json
        import tempfile
        from pathlib import Path
        from ddtriage.bootstrap import SessionState

        state = SessionState(bitlocker=True, bitlocker_partition="/dev/sdc3")
        with tempfile.TemporaryDirectory() as td:
            state.save(Path(td))
            loaded = SessionState.load(Path(td))
            assert loaded.bitlocker is True
            assert loaded.bitlocker_partition == "/dev/sdc3"
