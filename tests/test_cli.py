"""Tests for CLI argument parsing."""

import pytest
from unittest.mock import patch

from ddtriage.cli import main, _build_ddrescue_extra

# Use a nonexistent output-dir to prevent picking up real session state
NO_STATE = ["--output-dir", "/tmp/ddtriage_test_nonexistent"]


class TestCliParsing:
    def test_info_command(self):
        """Info command should fail gracefully with no image."""
        ret = main(["info", "--image", "/nonexistent/image.img"] + NO_STATE)
        assert ret == 1

    def test_status_command_no_mapfile(self):
        ret = main(["status", "--mapfile", "/nonexistent/sdb.log"] + NO_STATE)
        assert ret == 1

    def test_verbose_flag(self):
        """Verbose flag shouldn't crash."""
        ret = main(["-v", "status", "--mapfile", "/nonexistent/sdb.log"] + NO_STATE)
        assert ret == 1

    def test_tree_command_no_image(self):
        ret = main(["tree", "--image", "/nonexistent/image.img"] + NO_STATE)
        assert ret == 1

    def test_help_doesnt_crash(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0

    def test_scan_no_image(self):
        ret = main(["scan", "--image", "/nonexistent/image.img"] + NO_STATE)
        assert ret == 1


class TestBuildDdrescueExtra:
    def _make_args(self, **kwargs):
        """Create a mock args namespace."""
        from argparse import Namespace
        defaults = {
            'no_trim': False, 'no_scrape': False, 'reverse': False,
            'ddrescue_timeout': None, 'min_read_rate': None,
            'ddrescue_opts': '',
        }
        defaults.update(kwargs)
        return Namespace(**defaults)

    def test_empty_by_default(self):
        assert _build_ddrescue_extra(self._make_args()) == []

    def test_no_trim(self):
        extra = _build_ddrescue_extra(self._make_args(no_trim=True))
        assert '--no-trim' in extra

    def test_no_scrape(self):
        extra = _build_ddrescue_extra(self._make_args(no_scrape=True))
        assert '-n' in extra

    def test_reverse(self):
        extra = _build_ddrescue_extra(self._make_args(reverse=True))
        assert '-R' in extra

    def test_timeout(self):
        extra = _build_ddrescue_extra(self._make_args(ddrescue_timeout=30))
        assert extra == ['-T', '30s']

    def test_min_read_rate(self):
        extra = _build_ddrescue_extra(self._make_args(min_read_rate='1M'))
        assert extra == ['-a', '1M']

    def test_freeform_opts(self):
        extra = _build_ddrescue_extra(self._make_args(ddrescue_opts='-K 100M --pause-on-error=5'))
        assert '-K' in extra
        assert '100M' in extra
        assert '--pause-on-error=5' in extra

    def test_combined(self):
        extra = _build_ddrescue_extra(self._make_args(
            no_trim=True, reverse=True, ddrescue_opts='-c 64',
        ))
        assert '--no-trim' in extra
        assert '-R' in extra
        assert '-c' in extra
        assert '64' in extra
