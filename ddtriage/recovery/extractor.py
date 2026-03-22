"""Phase 5: File extraction — read file content from image and write to output."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..mapfile.parser import Mapfile
from ..mapfile.query import is_range_rescued
from ..ntfs.lznt1 import decompress_compression_unit, COMPRESSION_UNIT_SIZE
from ..ntfs.tree import FileRecord, DirectoryTree, ROOT_MFT_INDEX

log = logging.getLogger(__name__)


@dataclass
class ExtractionGap:
    """A gap in the recovered data for a file."""
    byte_offset: int    # within the file
    length: int         # bytes missing


@dataclass
class ExtractionResult:
    """Result of extracting a single file."""
    mft_index: int
    name: str
    source_path: str        # full NTFS path
    output_path: str        # where it was written
    size: int               # expected file size
    bytes_written: int
    complete: bool
    gaps: list[ExtractionGap] = field(default_factory=list)
    error: str | None = None


def _resolve_output_path(record: FileRecord, output_dir: Path) -> Path:
    """Compute the output file path preserving directory structure."""
    # Walk parent chain to build path components
    parts: list[str] = []
    node: FileRecord | None = record
    visited: set[int] = set()
    while node is not None and node.mft_index not in visited:
        visited.add(node.mft_index)
        if node.mft_index == ROOT_MFT_INDEX:
            break
        parts.append(node.name)
        node = getattr(node, '_parent', None)
    parts.reverse()

    if not parts:
        parts = [record.name]

    return output_dir.joinpath(*parts)


def extract_file(
    record: FileRecord,
    image_path: str,
    output_dir: Path,
    cluster_size: int,
    partition_offset: int,
    mapfile: Mapfile | None = None,
) -> ExtractionResult:
    """Extract a single file from the image.

    Reads data using the file's data runs (or resident data) and writes
    to the output directory, preserving the directory structure.
    """
    dest = _resolve_output_path(record, output_dir)
    source_path = record.full_path

    result = ExtractionResult(
        mft_index=record.mft_index,
        name=record.name,
        source_path=source_path,
        output_path=str(dest),
        size=record.size,
        bytes_written=0,
        complete=False,
    )

    # Create parent directories
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result.error = f"Could not create directory: {e}"
        return result

    # Case 1: Resident data (small file stored in MFT)
    if record.resident_data is not None:
        data = record.resident_data[:record.size]
        try:
            dest.write_bytes(data)
            result.bytes_written = len(data)
            result.complete = True
        except OSError as e:
            result.error = f"Write failed: {e}"
        _set_timestamps(dest, record)
        return result

    # Case 2: No data runs — empty file or metadata-only
    if not record.data_runs:
        if record.size == 0:
            try:
                dest.write_bytes(b'')
                result.complete = True
            except OSError as e:
                result.error = f"Write failed: {e}"
            _set_timestamps(dest, record)
            return result
        result.error = "No data runs and non-zero size"
        return result

    # Case 3: Non-resident — read from image via data runs
    try:
        output_data = _read_data_runs(
            record, image_path, cluster_size, partition_offset, mapfile, result,
        )
    except OSError as e:
        result.error = f"Image read failed: {e}"
        return result

    # Decompress if the file is LZNT1-compressed
    if record.is_compressed and output_data:
        try:
            output_data = _decompress_file(output_data, cluster_size)
        except Exception as e:
            log.warning("MFT %d (%s): decompression failed: %s",
                        record.mft_index, record.name, e)
            result.error = f"Decompression failed: {e}"
            # Still write the raw data as a best-effort fallback

    # Trim to actual file size
    output_data = output_data[:record.size]

    try:
        dest.write_bytes(output_data)
        result.bytes_written = len(output_data)
        result.complete = len(result.gaps) == 0 and len(output_data) == record.size
    except OSError as e:
        result.error = f"Write failed: {e}"

    _set_timestamps(dest, record)
    return result


def _read_data_runs(
    record: FileRecord,
    image_path: str,
    cluster_size: int,
    partition_offset: int,
    mapfile: Mapfile | None,
    result: ExtractionResult,
) -> bytes:
    """Read file data from the image using data runs."""
    output = bytearray()
    file_offset = 0  # current position within the logical file

    with open(image_path, 'rb') as img:
        for lcn, cluster_count in record.data_runs:
            run_bytes = cluster_count * cluster_size

            if lcn is None:
                # Sparse run — zeros
                output.extend(b'\x00' * run_bytes)
                file_offset += run_bytes
                continue

            disk_offset = lcn * cluster_size + partition_offset

            # Check mapfile for gaps
            if mapfile and not is_range_rescued(mapfile, disk_offset, run_bytes):
                log.warning(
                    "MFT %d (%s): gap at disk offset 0x%X, %d bytes",
                    record.mft_index, record.name, disk_offset, run_bytes,
                )
                result.gaps.append(ExtractionGap(
                    byte_offset=file_offset,
                    length=run_bytes,
                ))

            img.seek(disk_offset)
            data = img.read(run_bytes)

            if len(data) < run_bytes:
                # Image file shorter than expected — pad with zeros
                data += b'\x00' * (run_bytes - len(data))
                log.warning(
                    "MFT %d (%s): short read at 0x%X (got %d, expected %d)",
                    record.mft_index, record.name, disk_offset,
                    len(data) - (run_bytes - len(data)), run_bytes,
                )

            output.extend(data)
            file_offset += run_bytes

    return bytes(output)


def _decompress_file(raw_data: bytes, cluster_size: int) -> bytes:
    """Decompress an NTFS LZNT1-compressed file.

    NTFS compression works in compression units (typically 16 clusters).
    The data runs alternate between compressed runs and sparse runs.
    A sparse run within a compressed file means the preceding data
    decompresses to fill the full compression unit.
    """
    comp_unit_clusters = 16
    comp_unit_size = comp_unit_clusters * cluster_size
    output = bytearray()

    pos = 0
    while pos < len(raw_data):
        chunk = raw_data[pos:pos + comp_unit_size]
        if len(chunk) == comp_unit_size:
            # Full-size unit — stored uncompressed
            output.extend(chunk)
        elif len(chunk) == 0:
            # Sparse — all zeros
            output.extend(b'\x00' * comp_unit_size)
        else:
            # Compressed unit — decompress
            decompressed = decompress_compression_unit(chunk, comp_unit_size)
            output.extend(decompressed)
        pos += comp_unit_size

    return bytes(output)


def _set_timestamps(path: Path, record: FileRecord) -> None:
    """Set file modification/access times if available."""
    try:
        mtime = record.modified.timestamp() if record.modified else None
        atime = record.created.timestamp() if record.created else mtime
        if mtime is not None:
            os.utime(path, (atime or mtime, mtime))
    except (OSError, OverflowError, ValueError):
        pass  # best effort


def extract_selected(
    selected_indices: set[int],
    tree: DirectoryTree,
    image_path: str,
    output_dir: Path,
    cluster_size: int,
    partition_offset: int,
    mapfile: Mapfile | None = None,
) -> list[ExtractionResult]:
    """Extract all selected files to the output directory.

    Skips directories (they're created implicitly from file paths).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExtractionResult] = []

    files = [
        tree.all_records[idx]
        for idx in sorted(selected_indices)
        if idx in tree.all_records and not tree.all_records[idx].is_directory
    ]

    try:
        from ..progress import extraction_progress
        use_progress = len(files) > 20
    except ImportError:
        use_progress = False

    if use_progress:
        from ..progress import extraction_progress
        with extraction_progress(len(files)) as update:
            for record in files:
                result = extract_file(
                    record, image_path, output_dir,
                    cluster_size, partition_offset, mapfile,
                )
                results.append(result)
                update(1)
    else:
        for i, record in enumerate(files, 1):
            print(f"  [{i}/{len(files)}] {record.full_path}")
            result = extract_file(
                record, image_path, output_dir,
                cluster_size, partition_offset, mapfile,
            )
            results.append(result)

            if result.error:
                print(f"           ERROR: {result.error}")
            elif not result.complete:
                print(f"           PARTIAL: {len(result.gaps)} gap(s)")

    return results


def write_report(
    results: list[ExtractionResult],
    output_dir: Path,
) -> None:
    """Write a recovery report summarizing extraction results."""
    report_path = output_dir / "recovery_report.txt"

    complete = [r for r in results if r.complete]
    partial = [r for r in results if not r.complete and not r.error and r.bytes_written > 0]
    failed = [r for r in results if r.error or r.bytes_written == 0]

    with open(report_path, 'w') as f:
        f.write("ddtriage Recovery Report\n")
        f.write("=" * 60 + "\n\n")

        f.write(f"Total files: {len(results)}\n")
        f.write(f"  Complete:  {len(complete)}\n")
        f.write(f"  Partial:   {len(partial)}\n")
        f.write(f"  Failed:    {len(failed)}\n\n")

        if complete:
            f.write("--- Complete ---\n")
            for r in complete:
                f.write(f"  {r.source_path}  ({r.size} bytes)\n")
            f.write("\n")

        if partial:
            f.write("--- Partial (data gaps) ---\n")
            for r in partial:
                f.write(f"  {r.source_path}  ({r.bytes_written}/{r.size} bytes)\n")
                for gap in r.gaps:
                    f.write(f"    gap at file offset 0x{gap.byte_offset:X}, "
                            f"{gap.length} bytes\n")
            f.write("\n")

        if failed:
            f.write("--- Failed ---\n")
            for r in failed:
                f.write(f"  {r.source_path}  — {r.error or 'no data'}\n")
            f.write("\n")

    print(f"  Report written to {report_path}")
