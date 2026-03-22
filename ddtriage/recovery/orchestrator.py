"""Phase 4: Targeted recovery — coordinate gddrescue calls for selected files."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..mapfile.generator import generate_targeted_domain, merge_ranges
from ..mapfile.parser import Mapfile, parse_mapfile_from_path
from ..mapfile.query import coverage_percentage
from ..ntfs.data_runs import data_runs_to_byte_ranges
from ..ntfs.tree import FileRecord, DirectoryTree

log = logging.getLogger(__name__)


@dataclass
class RecoveryPlan:
    """Summary of what a targeted recovery pass will do."""
    file_count: int
    total_data_bytes: int       # total bytes across all selected files' data runs
    bytes_already_rescued: int
    bytes_to_read: int
    range_count: int            # number of distinct byte ranges to read
    domain_log_path: str


@dataclass
class FileRecoveryStatus:
    """Per-file result after a recovery pass."""
    mft_index: int
    name: str
    path: str
    size: int
    coverage_pct: float         # after recovery
    complete: bool


def collect_byte_ranges(
    records: list[FileRecord],
    cluster_size: int,
    partition_offset: int = 0,
) -> list[tuple[int, int]]:
    """Collect all non-sparse byte ranges needed for the given file records.

    Returns merged list of (byte_offset, byte_length) tuples.
    """
    raw_ranges: list[tuple[int, int]] = []

    for rec in records:
        if rec.resident_data is not None:
            continue  # data already in MFT, no disk read needed
        if not rec.data_runs:
            continue

        for offset, length in data_runs_to_byte_ranges(
            rec.data_runs, cluster_size, partition_offset,
        ):
            if offset is not None:
                raw_ranges.append((offset, length))

    return merge_ranges(raw_ranges)


def plan_recovery(
    selected_indices: set[int],
    tree: DirectoryTree,
    mapfile: Mapfile,
    cluster_size: int,
    partition_offset: int,
    work_dir: Path,
) -> RecoveryPlan:
    """Build a recovery plan: compute what needs to be read from disk.

    Generates the domain logfile and returns statistics.
    """
    # Collect file records (skip directories — they have no file data)
    records = [
        tree.all_records[idx]
        for idx in selected_indices
        if idx in tree.all_records and not tree.all_records[idx].is_directory
    ]

    all_ranges = collect_byte_ranges(records, cluster_size, partition_offset)
    total_data = sum(length for _, length in all_ranges)

    domain_path = work_dir / "recovery_domain.log"
    rescued, to_read = generate_targeted_domain(
        all_ranges, mapfile, str(domain_path),
    )

    # Count distinct ranges in the written domain log
    range_count = 0
    if domain_path.exists():
        dm = parse_mapfile_from_path(str(domain_path))
        range_count = len(dm.entries)

    return RecoveryPlan(
        file_count=len(records),
        total_data_bytes=total_data,
        bytes_already_rescued=rescued,
        bytes_to_read=to_read,
        range_count=range_count,
        domain_log_path=str(domain_path),
    )


def print_plan(plan: RecoveryPlan) -> None:
    """Print a human-readable summary of the recovery plan."""
    print(f"\n  Recovery plan:")
    print(f"    Files to recover:     {plan.file_count}")
    print(f"    Total data on disk:   {_h(plan.total_data_bytes)}")
    print(f"    Already in image:     {_h(plan.bytes_already_rescued)}")
    print(f"    Bytes to read:        {_h(plan.bytes_to_read)}")
    print(f"    Distinct ranges:      {plan.range_count}")
    print()


def run_recovery(
    plan: RecoveryPlan,
    device: str,
    image_path: str,
    mapfile_path: str,
    retry: int = 0,
    dry_run: bool = False,
    ddrescue_extra: list[str] | None = None,
) -> bool:
    """Execute gddrescue with the domain log from the recovery plan.

    Returns True on success.
    """
    extra = ddrescue_extra or []

    if plan.bytes_to_read == 0:
        print("  All data already in image — nothing to read from disk.")
        return True

    cmd = [
        "ddrescue", "-d",
        "-m", plan.domain_log_path,
    ] + extra + [
        device, image_path, mapfile_path,
    ]

    if dry_run:
        print(f"  [dry-run] Would execute: {' '.join(cmd)}")
        return True

    log.info("Running: %s", " ".join(cmd))
    print(f"  Running gddrescue ({_h(plan.bytes_to_read)} to read)...")

    result = subprocess.run(cmd, timeout=None)
    if result.returncode != 0:
        log.error("gddrescue failed with exit code %d", result.returncode)
        print(f"  gddrescue exited with code {result.returncode}")
        return False

    # Optional retry pass
    if retry > 0:
        retry_cmd = [
            "ddrescue", "-d", f"-r{retry}",
            "-m", plan.domain_log_path,
        ] + extra + [
            device, image_path, mapfile_path,
        ]
        log.info("Retry pass: %s", " ".join(retry_cmd))
        print(f"  Retry pass ({retry} retries)...")
        result = subprocess.run(retry_cmd, timeout=None)
        if result.returncode != 0:
            log.warning("Retry pass exited with code %d", result.returncode)

    print("  gddrescue pass complete.")
    return True


def assess_results(
    selected_indices: set[int],
    tree: DirectoryTree,
    mapfile: Mapfile,
    cluster_size: int,
    partition_offset: int,
) -> list[FileRecoveryStatus]:
    """After recovery, assess per-file status."""
    results: list[FileRecoveryStatus] = []

    for idx in sorted(selected_indices):
        rec = tree.all_records.get(idx)
        if rec is None or rec.is_directory:
            continue

        if rec.resident_data is not None:
            results.append(FileRecoveryStatus(
                mft_index=idx, name=rec.name, path=rec.full_path,
                size=rec.size, coverage_pct=100.0, complete=True,
            ))
            continue

        if not rec.data_runs:
            results.append(FileRecoveryStatus(
                mft_index=idx, name=rec.name, path=rec.full_path,
                size=rec.size, coverage_pct=0.0, complete=False,
            ))
            continue

        byte_ranges = data_runs_to_byte_ranges(
            rec.data_runs, cluster_size, partition_offset,
        )
        total = 0
        rescued = 0
        for offset, length in byte_ranges:
            if offset is None:
                continue
            total += length
            pct = coverage_percentage(mapfile, offset, length)
            rescued += int(length * pct / 100.0)

        pct = (rescued / total * 100.0) if total > 0 else 0.0
        results.append(FileRecoveryStatus(
            mft_index=idx, name=rec.name, path=rec.full_path,
            size=rec.size, coverage_pct=pct, complete=pct >= 100.0,
        ))

    return results


def print_results(results: list[FileRecoveryStatus]) -> None:
    """Print per-file recovery results."""
    complete = sum(1 for r in results if r.complete)
    partial = sum(1 for r in results if not r.complete and r.coverage_pct > 0)
    failed = sum(1 for r in results if r.coverage_pct == 0 and not r.complete)

    print(f"\n  Recovery results: {complete} complete, {partial} partial, {failed} failed")
    print()

    for r in results:
        if r.complete:
            marker = " OK "
        elif r.coverage_pct > 0:
            marker = f"{r.coverage_pct:3.0f}%"
        else:
            marker = "FAIL"
        print(f"    [{marker}]  {r.path}  ({_h(r.size)})")

    print()


def _h(nbytes: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != 'B' else f"{nbytes} B"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} PB"
