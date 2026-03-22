"""Tests for domain logfile generation."""

import os
import tempfile
import pytest

from ddtriage.mapfile.parser import parse_mapfile
from ddtriage.mapfile.generator import (
    merge_ranges, subtract_rescued, generate_domain_log, generate_targeted_domain,
)


class TestMergeRanges:
    def test_empty(self):
        assert merge_ranges([]) == []

    def test_no_overlap(self):
        ranges = [(0, 100), (200, 100)]
        assert merge_ranges(ranges) == [(0, 100), (200, 100)]

    def test_adjacent(self):
        ranges = [(0, 100), (100, 100)]
        assert merge_ranges(ranges) == [(0, 200)]

    def test_overlapping(self):
        ranges = [(0, 150), (100, 150)]
        assert merge_ranges(ranges) == [(0, 250)]

    def test_unsorted_input(self):
        ranges = [(200, 50), (0, 100), (100, 50)]
        result = merge_ranges(ranges)
        assert result == [(0, 150), (200, 50)]

    def test_fully_contained(self):
        ranges = [(0, 1000), (100, 200)]
        assert merge_ranges(ranges) == [(0, 1000)]

    def test_zero_length_filtered(self):
        ranges = [(0, 100), (200, 0), (300, 100)]
        assert merge_ranges(ranges) == [(0, 100), (300, 100)]

    def test_many_fragments(self):
        ranges = [(i * 10, 15) for i in range(10)]
        result = merge_ranges(ranges)
        # All overlap: 0-15, 10-25, 20-35, ... 90-105
        assert result == [(0, 105)]


SAMPLE_MAPFILE = """\
# test mapfile
0x00000000  +
0x00000000  0x00100000  +
0x00100000  0x00100000  *
0x00200000  0x00100000  +
0x00300000  0x00100000  ?
"""


class TestSubtractRescued:
    def setup_method(self):
        self.mf = parse_mapfile(SAMPLE_MAPFILE)

    def test_fully_rescued_subtracted(self):
        # Request a range entirely within the first rescued block
        result = subtract_rescued([(0, 0x80000)], self.mf)
        assert result == []

    def test_non_rescued_kept(self):
        # Request range in the non-trimmed block
        result = subtract_rescued([(0x100000, 0x100000)], self.mf)
        assert result == [(0x100000, 0x100000)]

    def test_spanning_rescued_and_non_rescued(self):
        # 0x00000-0x200000: first half rescued, second half not
        result = subtract_rescued([(0, 0x200000)], self.mf)
        # Should keep 0x100000-0x200000 (the non-trimmed block)
        assert result == [(0x100000, 0x100000)]

    def test_multiple_ranges(self):
        result = subtract_rescued([
            (0, 0x100000),       # fully rescued
            (0x300000, 0x50000), # non-tried
        ], self.mf)
        assert result == [(0x300000, 0x50000)]

    def test_partial_overlap(self):
        # Range spans rescued + non-rescued + rescued
        result = subtract_rescued([(0x80000, 0x200000)], self.mf)
        # 0x80000-0x100000 rescued (skip), 0x100000-0x200000 not, 0x200000-0x280000 rescued (skip)
        assert result == [(0x100000, 0x100000)]


class TestGenerateDomainLog:
    def test_writes_valid_mapfile(self):
        with tempfile.NamedTemporaryFile(mode='r', suffix='.log', delete=False) as f:
            path = f.name
        try:
            generate_domain_log([(0x1000, 0x2000), (0x5000, 0x1000)], path)

            with open(path) as f:
                content = f.read()

            # Should be parseable; includes gap-filling '?' entries
            mf = parse_mapfile(content)
            plus_entries = [e for e in mf.entries if e.status == '+']
            assert len(plus_entries) == 2
            assert plus_entries[0].pos == 0x1000
            assert plus_entries[0].size == 0x2000
            assert plus_entries[1].pos == 0x5000
        finally:
            os.unlink(path)

    def test_merges_before_writing(self):
        with tempfile.NamedTemporaryFile(mode='r', suffix='.log', delete=False) as f:
            path = f.name
        try:
            # Adjacent ranges should merge
            generate_domain_log([(0x1000, 0x1000), (0x2000, 0x1000)], path)

            with open(path) as f:
                content = f.read()

            mf = parse_mapfile(content)
            plus_entries = [e for e in mf.entries if e.status == '+']
            assert len(plus_entries) == 1
            assert plus_entries[0].pos == 0x1000
            assert plus_entries[0].size == 0x2000
        finally:
            os.unlink(path)


class TestGenerateTargetedDomain:
    def test_excludes_rescued(self):
        mapfile_text = SAMPLE_MAPFILE
        mf = parse_mapfile(mapfile_text)

        with tempfile.NamedTemporaryFile(mode='r', suffix='.log', delete=False) as f:
            path = f.name
        try:
            rescued, to_read = generate_targeted_domain(
                [(0, 0x400000)],  # request entire 4 blocks
                mf,
                path,
            )

            # Blocks: 0-100000 rescued, 100000-200000 not, 200000-300000 rescued, 300000-400000 not
            assert rescued == 0x200000  # two rescued blocks
            assert to_read == 0x200000  # two un-rescued blocks

            with open(path) as f:
                content = f.read()
            domain = parse_mapfile(content)
            # Should contain the two un-rescued '+' ranges (plus '?' gap fillers)
            plus_entries = [e for e in domain.entries if e.status == '+']
            assert len(plus_entries) == 2
        finally:
            os.unlink(path)
