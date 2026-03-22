"""Tests for gddrescue mapfile parsing and querying."""

import pytest

from ddtriage.mapfile.parser import parse_mapfile, Mapfile, MapEntry
from ddtriage.mapfile.query import is_range_rescued, get_range_status, coverage_percentage


SAMPLE_MAPFILE = """\
# Rescue Logfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue -d /dev/sdb ./sdb.img ./sdb.log
# Start time:   2024-01-15 10:30:00
# current_pos  current_status
0x00180000     +
#      pos        size  status
0x00000000  0x00100000  +
0x00100000  0x00080000  *
0x00180000  0x00080000  -
0x00200000  0x7FE00000  ?
"""


class TestMapfileParser:
    def test_parse_sample(self):
        mf = parse_mapfile(SAMPLE_MAPFILE)
        assert len(mf.comments) == 5
        assert mf.current_pos == 0x00180000
        assert mf.current_status == '+'
        assert len(mf.entries) == 4

    def test_entry_values(self):
        mf = parse_mapfile(SAMPLE_MAPFILE)
        e = mf.entries[0]
        assert e.pos == 0x00000000
        assert e.size == 0x00100000
        assert e.status == '+'
        assert e.end == 0x00100000

    def test_all_statuses(self):
        mf = parse_mapfile(SAMPLE_MAPFILE)
        statuses = [e.status for e in mf.entries]
        assert statuses == ['+', '*', '-', '?']

    def test_decimal_values(self):
        text = "0  +\n0  1048576  +\n"
        mf = parse_mapfile(text)
        assert mf.entries[0].pos == 0
        assert mf.entries[0].size == 1048576

    def test_invalid_status(self):
        text = "0  +\n0  100  X\n"
        with pytest.raises(ValueError, match="Unknown status"):
            parse_mapfile(text)

    def test_empty_mapfile(self):
        mf = parse_mapfile("# just comments\n")
        assert len(mf.entries) == 0

    def test_find_entry(self):
        mf = parse_mapfile(SAMPLE_MAPFILE)
        # 0x50000 is within first entry [0, 0x100000)
        idx = mf.find_entry(0x50000)
        assert idx == 0
        assert mf.entries[idx].status == '+'

        # 0x150000 is within second entry [0x100000, 0x180000)
        idx = mf.find_entry(0x150000)
        assert idx == 1
        assert mf.entries[idx].status == '*'

        # Within the last '?' entry
        idx = mf.find_entry(0x00300000)
        assert idx == 3
        assert mf.entries[idx].status == '?'

        # Past the end of all entries
        idx = mf.find_entry(0xFFFFFFFF)
        assert idx == -1


class TestMapfileQuery:
    def setup_method(self):
        self.mf = parse_mapfile(SAMPLE_MAPFILE)

    def test_fully_rescued_range(self):
        # First 0x100000 bytes are rescued
        assert is_range_rescued(self.mf, 0, 0x100000) is True

    def test_partially_rescued_range(self):
        # Spans rescued + non-trimmed
        assert is_range_rescued(self.mf, 0, 0x180000) is False

    def test_non_rescued_range(self):
        # In the non-trimmed region
        assert is_range_rescued(self.mf, 0x100000, 0x80000) is False

    def test_zero_length(self):
        assert is_range_rescued(self.mf, 0, 0) is True

    def test_get_range_status_single_entry(self):
        statuses = get_range_status(self.mf, 0, 0x80000)
        assert len(statuses) == 1
        assert statuses[0].status == '+'
        assert statuses[0].size == 0x80000

    def test_get_range_status_spanning(self):
        # Spans first two entries
        statuses = get_range_status(self.mf, 0x80000, 0x100000)
        assert len(statuses) == 2
        assert statuses[0].status == '+'
        assert statuses[0].pos == 0x80000
        assert statuses[0].size == 0x80000  # rest of first entry
        assert statuses[1].status == '*'
        assert statuses[1].pos == 0x100000

    def test_coverage_fully_rescued(self):
        pct = coverage_percentage(self.mf, 0, 0x100000)
        assert pct == 100.0

    def test_coverage_half_rescued(self):
        # 0x00000-0x100000 is rescued, 0x100000-0x200000 is not
        pct = coverage_percentage(self.mf, 0, 0x200000)
        # 0x100000 rescued out of 0x200000 = 50%
        assert pct == 50.0

    def test_coverage_zero_length(self):
        assert coverage_percentage(self.mf, 0, 0) == 100.0

    def test_range_beyond_entries(self):
        # Range starting past all entries
        statuses = get_range_status(self.mf, 0x90000000, 0x1000)
        assert len(statuses) == 1
        assert statuses[0].status == '?'
