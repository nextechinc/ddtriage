"""Main CLI entry point for ddtriage."""

from __future__ import annotations

import argparse
import logging
import mmap
import sys
from pathlib import Path

log = logging.getLogger(__name__)

from .bootstrap import (
    SessionState, check_dependencies, list_disks, partition_basename,
    select_disk_interactive, select_partition_interactive, run_bootstrap,
)
from .mapfile.parser import parse_mapfile_from_path
from .ntfs.boot_sector import parse_boot_sector
from .ntfs.mft_parser import iter_mft_records
from .ntfs.tree import build_tree
from .recovery.orchestrator import (
    plan_recovery, print_plan, run_recovery, assess_results, print_results,
)
from .recovery.extractor import extract_selected, write_report
from .selection import import_selection, collect_selection_with_children


SUBCOMMANDS = {"bootstrap", "scan", "browse", "recover", "extract",
               "info", "status", "tree"}


def main(argv: list[str] | None = None) -> int:
    # Pre-parse: if the first non-flag arg looks like a device path (not a
    # subcommand), rewrite it as the interactive workflow so argparse doesn't
    # choke trying to match it as a subcommand.
    raw = argv if argv is not None else sys.argv[1:]
    # Pre-parse: if there's a /dev/... path on the command line without a
    # subcommand, extract it so argparse doesn't try to match it as one.
    _device_override = None
    argv_clean = list(raw)
    for i, arg in enumerate(raw):
        if arg.startswith('/dev/') and arg not in SUBCOMMANDS:
            _device_override = arg
            argv_clean.pop(argv_clean.index(arg))
            break
    argv = argv_clean

    # Shared parent parser for global options
    parent = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parent.add_argument("--image", default=None,
                        help="Path to the disk image file (default: <partition>.img)")
    parent.add_argument("--mapfile", default=None,
                        help="Path to the gddrescue mapfile (default: <partition>.log)")
    parent.add_argument("--offset", type=int, default=None,
                        help="Partition offset in bytes (auto-detected if omitted)")
    parent.add_argument("--output-dir", default="./",
                        help="Working directory for image/log/temp files (default: ./)")
    parent.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity")
    parent.add_argument("--dry-run", action="store_true",
                        help="Show what would be done without running gddrescue")
    parent.add_argument("--bitlocker-password", default=None,
                        help="BitLocker password for encrypted volumes")
    parent.add_argument("--recovery-key", default=None,
                        help="BitLocker 48-digit recovery key")

    # ddrescue tuning options
    parent.add_argument("--ddrescue-opts", default="",
                        help="Extra flags passed directly to ddrescue (e.g. \"-K 100M\")")
    parent.add_argument("--no-trim", action="store_true",
                        help="Skip ddrescue trimming phase (faster on very bad drives)")
    parent.add_argument("--no-scrape", action="store_true",
                        help="Skip ddrescue scraping phase")
    parent.add_argument("--reverse", action="store_true",
                        help="Reverse direction of recovery passes")
    parent.add_argument("--timeout", type=int, default=None, dest="ddrescue_timeout",
                        help="Max seconds since last successful read before giving up")
    parent.add_argument("--min-read-rate", default=None,
                        help="Minimum read rate (e.g. 1M) below which ddrescue switches areas")

    parser = argparse.ArgumentParser(
        prog="ddtriage",
        description="NTFS-aware selective data recovery tool",
        parents=[parent],
    )

    sub = parser.add_subparsers(dest="command")

    # --- bootstrap ---
    p_boot = sub.add_parser("bootstrap", parents=[parent],
                            help="Bootstrap: recover MFT from failing disk")
    p_boot.add_argument("device", nargs="?", help="Source device (e.g., /dev/sdb)")
    p_boot.add_argument("--retry", type=int, default=0,
                        help="Number of gddrescue retry passes")

    # --- scan ---
    sub.add_parser("scan", parents=[parent],
                   help="Parse MFT from image (no disk I/O)")

    # --- browse ---
    p_browse = sub.add_parser("browse", parents=[parent],
                              help="Interactive file browser")
    p_browse.add_argument("--show-deleted", action="store_true",
                          help="Include deleted entries in browser")

    # --- recover ---
    p_recover = sub.add_parser("recover", parents=[parent],
                               help="Recover selected files from disk")
    p_recover.add_argument("--selection", required=True,
                           help="Path to selection JSON file")
    p_recover.add_argument("--output", default="./recovered",
                           help="Output directory for recovered files")
    p_recover.add_argument("--retry", type=int, default=0,
                           help="Number of gddrescue retry passes")
    p_recover.add_argument("--device", help="Source device (overrides state)")

    # --- extract ---
    p_extract = sub.add_parser("extract", parents=[parent],
                               help="Extract files from image (no disk I/O)")
    p_extract.add_argument("--selection", required=True,
                           help="Path to selection JSON file")
    p_extract.add_argument("--output", default="./recovered",
                           help="Output directory for extracted files")

    # --- info ---
    sub.add_parser("info", parents=[parent],
                   help="Show boot sector and MFT info")

    # --- status ---
    sub.add_parser("status", parents=[parent],
                   help="Show mapfile coverage summary")

    # --- tree ---
    p_tree = sub.add_parser("tree", parents=[parent],
                            help="Dump directory tree to stdout")
    p_tree.add_argument("--show-deleted", action="store_true",
                        help="Include deleted entries")
    p_tree.add_argument("--show-system", action="store_true",
                        help="Include system metadata entries")

    args = parser.parse_args(argv)

    # Workaround: argparse with parents=[parent] on subparsers causes the
    # subparser's defaults to overwrite values parsed by the main parser.
    # Re-parse global options to recover any that were specified before the
    # subcommand name (e.g. "ddtriage --recovery-key X recover ...").
    global_args, _ = parent.parse_known_args(argv)
    for key, val in vars(global_args).items():
        if val is not None and val != parent.get_default(key):
            setattr(args, key, val)

    # Inject device from pre-parse if no subcommand was given
    if not hasattr(args, 'device') or args.command is None:
        args.device = _device_override

    # Configure logging
    level = logging.WARNING
    if args.verbose >= 1:
        level = logging.INFO
    if args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(name)s: %(message)s")

    work_dir = Path(args.output_dir)

    # Dispatch
    if args.command == "bootstrap":
        return cmd_bootstrap(args, work_dir)
    elif args.command == "scan":
        return cmd_scan(args, work_dir)
    elif args.command == "browse":
        return cmd_browse(args, work_dir)
    elif args.command == "recover":
        return cmd_recover(args, work_dir)
    elif args.command == "extract":
        return cmd_extract(args, work_dir)
    elif args.command == "info":
        return cmd_info(args, work_dir)
    elif args.command == "status":
        return cmd_status(args, work_dir)
    elif args.command == "tree":
        return cmd_tree(args, work_dir)
    else:
        # Full interactive workflow
        return cmd_interactive(args, work_dir)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_bootstrap(args, work_dir: Path) -> int:
    missing = check_dependencies()
    if "ddrescue" in missing:
        print("Error: gddrescue (ddrescue) is required but not installed.")
        return 1

    if args.device:
        # Device given directly — still need to select partition
        from .bootstrap import PartitionInfo, DiskInfo
        disks = list_disks()
        disk = None
        for d in disks:
            if d.path == args.device:
                disk = d
                break
        if disk is None:
            # Might be a partition directly
            part = PartitionInfo(
                path=args.device, number=0,
                start_bytes=args.offset or 0,
                size_bytes=0, size_human="",
                fs_type="ntfs", label="",
            )
        else:
            part = select_partition_interactive(disk)
            if part is None:
                return 1
    else:
        disks = list_disks()
        disk = select_disk_interactive(disks)
        if disk is None:
            return 1
        part = select_partition_interactive(disk)
        if part is None:
            return 1

    state = run_bootstrap(
        part, work_dir, retry=args.retry,
        bitlocker_password=getattr(args, 'bitlocker_password', None),
        bitlocker_recovery_key=getattr(args, 'recovery_key', None),
        ddrescue_extra=_build_ddrescue_extra(args),
    )
    return 0


def cmd_scan(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    tree = _parse_mft(state)
    if tree is None:
        return 1

    print(f"  Directory tree: {tree.total_files} files, {tree.total_dirs} directories")
    if tree.orphans:
        print(f"  Orphaned entries: {len(tree.orphans)}")

    # Update state
    state.mft_parse_complete = True
    state.total_mft_entries = len(tree.all_records)
    state.phase = "browse"
    state.save(work_dir)
    return 0


def cmd_browse(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    include_deleted = getattr(args, 'show_deleted', False)
    tree = _parse_mft(state, include_deleted=include_deleted)
    if tree is None:
        return 1

    mapfile = None
    mf_path = Path(state.mapfile)
    if mf_path.exists():
        mapfile = parse_mapfile_from_path(str(mf_path))

    from .tui.browser import run_browser
    selected = run_browser(
        dir_tree=tree,
        mapfile=mapfile,
        cluster_size=state.cluster_size,
        partition_offset=state.partition_offset,
        device_name=state.device,
        image_path=state.image,
        work_dir=str(work_dir),
        mft_coverage_pct=state.mft_coverage_pct or 100.0,
    )

    if selected:
        print(f"\n  {len(selected)} entries selected for recovery.")
        # Auto-save selection
        from .selection import export_selection
        sel_path = work_dir / "selection.json"
        export_selection(selected, tree, sel_path)
        print(f"  Selection saved to {sel_path}")

        # Offer to proceed to recovery
        if state.device:
            try:
                proceed = input("  Proceed with recovery? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if proceed != 'n':
                out_name = f"{partition_basename(state.partition)}-recovered" if state.partition else "recovered"
                return _do_recover(
                    selected, tree, state, work_dir,
                    output_dir=work_dir / out_name,
                    dry_run=args.dry_run,
                    ddrescue_extra=_build_ddrescue_extra(args),
                    bitlocker_password=getattr(args, 'bitlocker_password', None),
                    bitlocker_recovery_key=getattr(args, 'recovery_key', None),
                )
    return 0


def cmd_recover(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    if args.device:
        state.device = args.device

    selected = import_selection(args.selection)
    tree = _parse_mft(state)
    if tree is None:
        return 1

    expanded = collect_selection_with_children(selected, tree)
    return _do_recover(
        expanded, tree, state, work_dir,
        output_dir=Path(args.output),
        retry=args.retry,
        dry_run=args.dry_run,
        ddrescue_extra=_build_ddrescue_extra(args),
        bitlocker_password=getattr(args, 'bitlocker_password', None),
        bitlocker_recovery_key=getattr(args, 'recovery_key', None),
    )


def cmd_extract(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    selected = import_selection(args.selection)
    tree = _parse_mft(state)
    if tree is None:
        return 1

    expanded = collect_selection_with_children(selected, tree)

    mapfile = None
    mf_path = Path(state.mapfile)
    if mf_path.exists():
        mapfile = parse_mapfile_from_path(str(mf_path))

    output_dir = Path(args.output)
    print(f"\n  Extracting {len(expanded)} entries to {output_dir}...")
    results = extract_selected(
        expanded, tree, state.image, output_dir,
        state.cluster_size, state.partition_offset, mapfile,
    )
    write_report(results, output_dir)

    complete = sum(1 for r in results if r.complete)
    print(f"\n  Done. {complete}/{len(results)} files extracted successfully.")
    return 0


def cmd_info(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    image = Path(state.image)
    if not image.exists():
        print(f"Error: Image not found at {image}")
        return 1

    with open(image, 'rb') as f:
        f.seek(state.partition_offset)
        bs_data = f.read(512)

    try:
        bs = parse_boot_sector(bs_data)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    print(f"\n  NTFS Boot Sector Info:")
    print(f"    OEM ID:              {bs.oem_id}")
    print(f"    Bytes per sector:    {bs.bytes_per_sector}")
    print(f"    Sectors per cluster: {bs.sectors_per_cluster}")
    print(f"    Cluster size:        {bs.cluster_size}")
    print(f"    Total sectors:       {bs.total_sectors}")
    print(f"    MFT start LCN:      {bs.mft_start_lcn}")
    print(f"    MFT mirror LCN:     {bs.mft_mirror_lcn}")
    print(f"    MFT record size:    {bs.mft_record_size}")
    print(f"    Index block size:   {bs.index_block_size}")
    print(f"    Volume serial:      0x{bs.volume_serial:016X}")
    print(f"    Partition offset:   {state.partition_offset}")
    print(f"    MFT byte offset:    {bs.mft_offset(state.partition_offset)}")
    print()
    return 0


def cmd_status(args, work_dir: Path) -> int:
    mf_path = Path(args.mapfile)
    if not mf_path.exists():
        print(f"Error: Mapfile not found at {mf_path}")
        return 1

    mf = parse_mapfile_from_path(str(mf_path))

    from .mapfile.parser import STATUS_FINISHED, STATUS_BAD
    total = sum(e.size for e in mf.entries)
    rescued = sum(e.size for e in mf.entries if e.status == STATUS_FINISHED)
    bad = sum(e.size for e in mf.entries if e.status == STATUS_BAD)
    pct = (rescued / total * 100) if total > 0 else 0

    print(f"\n  Mapfile: {mf_path}")
    print(f"    Total mapped:  {_h(total)}")
    print(f"    Rescued (+):   {_h(rescued)} ({pct:.1f}%)")
    print(f"    Bad sectors:   {_h(bad)}")
    print(f"    Entries:       {len(mf.entries)}")
    print()
    return 0


def cmd_tree(args, work_dir: Path) -> int:
    state = _load_state_or_args(args, work_dir)
    tree = _parse_mft(state,
                      include_deleted=getattr(args, 'show_deleted', False),
                      include_system=getattr(args, 'show_system', False))
    if tree is None:
        return 1

    def _print_tree(record, indent: int = 0) -> None:
        prefix = "  " * indent
        icon = "DIR " if record.is_directory else "    "
        size = f"  ({_h(record.size)})" if not record.is_directory else ""
        deleted = " [DEL]" if record.is_deleted else ""
        print(f"  {prefix}{icon}{record.name}{size}{deleted}")
        for child in sorted(record.children, key=lambda c: (not c.is_directory, c.name.lower())):
            _print_tree(child, indent + 1)

    _print_tree(tree.root)
    if tree.orphans:
        print(f"\n  ORPHANS ({len(tree.orphans)}):")
        for o in tree.orphans:
            print(f"    {o.name}  ({_h(o.size)})")
    return 0


def cmd_interactive(args, work_dir: Path) -> int:
    """Full interactive workflow: bootstrap → scan → browse → recover → extract."""
    print("\n  ddtriage — NTFS-aware selective data recovery\n")

    # Check for existing state
    state = SessionState.load(work_dir)
    if state and state.bootstrap_complete:
        print(f"  Found existing session: {state.device} → {state.image}")
        try:
            resume = input("  Resume from existing session? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if resume == 'n':
            state = None

    # Phase 1: Bootstrap
    if state is None or not state.bootstrap_complete:
        missing = check_dependencies()
        if "ddrescue" in missing:
            print("  Error: gddrescue (ddrescue) is required. Install with:")
            print("    sudo apt install gddrescue")
            return 1

        if args.device:
            from .bootstrap import PartitionInfo
            # Assume it's a partition if offset given, otherwise detect
            if args.offset is not None:
                part = PartitionInfo(
                    path=args.device, number=0,
                    start_bytes=args.offset,
                    size_bytes=0, size_human="",
                    fs_type="ntfs", label="",
                )
            else:
                disks = list_disks()
                disk = None
                for d in disks:
                    if d.path == args.device:
                        disk = d
                        break
                    for p in d.partitions:
                        if p.path == args.device:
                            part = p
                            disk = d
                            break
                    if disk:
                        break
                if disk and not locals().get('part'):
                    part = select_partition_interactive(disk)
                    if part is None:
                        return 1
                elif not disk:
                    print(f"  Error: Device {args.device} not found.")
                    return 1
        else:
            disks = list_disks()
            if not disks:
                print("  No disks found. Are you running as root?")
                return 1
            disk = select_disk_interactive(disks)
            if disk is None:
                return 1
            part = select_partition_interactive(disk)
            if part is None:
                return 1

        state = run_bootstrap(
            part, work_dir,
            bitlocker_password=getattr(args, 'bitlocker_password', None),
            bitlocker_recovery_key=getattr(args, 'recovery_key', None),
            ddrescue_extra=_build_ddrescue_extra(args),
        )

    if not state.bootstrap_complete:
        print("  Bootstrap did not complete successfully.")
        return 1

    # Phase 2: Parse MFT
    print("\n  Parsing MFT...")
    tree = _parse_mft(state)
    if tree is None:
        print("  Error: Could not parse MFT from image.")
        return 1
    print(f"  Found {tree.total_files} files, {tree.total_dirs} directories")
    if tree.orphans:
        print(f"  ({len(tree.orphans)} orphaned entries)")

    # Phase 3: TUI browser
    mapfile = None
    mf_path = Path(state.mapfile)
    if mf_path.exists():
        mapfile = parse_mapfile_from_path(str(mf_path))

    from .tui.browser import run_browser
    selected = run_browser(
        dir_tree=tree,
        mapfile=mapfile,
        cluster_size=state.cluster_size,
        partition_offset=state.partition_offset,
        device_name=state.device,
        image_path=state.image,
        work_dir=str(work_dir),
        mft_coverage_pct=state.mft_coverage_pct or 100.0,
    )

    if not selected:
        print("  No files selected. Exiting.")
        return 0

    # Save selection
    from .selection import export_selection
    sel_path = work_dir / "selection.json"
    export_selection(selected, tree, sel_path)

    # Phase 4 + 5: Recovery and extraction
    out_name = f"{partition_basename(state.partition)}-recovered" if state.partition else "recovered"
    output_dir = work_dir / out_name
    return _do_recover(
        selected, tree, state, work_dir,
        output_dir=output_dir,
        dry_run=args.dry_run,
        ddrescue_extra=_build_ddrescue_extra(args),
        bitlocker_password=getattr(args, 'bitlocker_password', None),
        bitlocker_recovery_key=getattr(args, 'recovery_key', None),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state_or_args(args, work_dir: Path) -> SessionState:
    """Load session state from file, falling back to CLI args."""
    state = SessionState.load(work_dir)
    if state is None:
        state = SessionState(
            image=args.image or "",
            mapfile=args.mapfile or "",
            partition_offset=args.offset or 0,
        )
    # CLI args override state if explicitly provided
    if args.image is not None:
        state.image = args.image
    if args.mapfile is not None:
        state.mapfile = args.mapfile
    if args.offset is not None:
        state.partition_offset = args.offset
    return state


def _parse_mft(state: SessionState, include_deleted: bool = False, include_system: bool = False):
    """Parse MFT from image and build directory tree. Returns DirectoryTree or None."""
    image = Path(state.image)
    if not image.exists():
        print(f"  Error: Image not found at {image}")
        return None

    offset = state.partition_offset
    cluster_size = state.cluster_size or 4096
    record_size = state.mft_record_size or 1024

    from .ntfs.boot_sector import BitLockerDetected
    log.info("Reading boot sector from %s at offset 0x%X", image, offset)
    try:
        with open(image, 'rb') as f:
            f.seek(offset)
            bs_data = f.read(512)
        bs = parse_boot_sector(bs_data)
        state.cluster_size = bs.cluster_size
        state.mft_record_size = bs.mft_record_size
        cluster_size = bs.cluster_size
        record_size = bs.mft_record_size
        mft_offset = bs.mft_offset(offset)
        log.info("Boot sector: cluster_size=%d, mft_lcn=%d, mft_offset=0x%X",
                 cluster_size, bs.mft_start_lcn, mft_offset)
    except BitLockerDetected:
        print(f"  Error: {image} is BitLocker-encrypted.")
        print(f"  Hint: re-run bootstrap with --recovery-key to create a decrypted image.")
        return None
    except (ValueError, OSError) as e:
        if state.mft_start_lcn:
            mft_offset = state.mft_start_lcn * cluster_size + offset
            print(f"  Warning: boot sector unreadable ({e}), using saved state "
                  f"(MFT at 0x{mft_offset:X})")
        else:
            print(f"  Error: boot sector unreadable and no saved MFT location: {e}")
            print(f"  Hint: re-run bootstrap to recover the boot sector.")
            return None

    # Memory-map the image for efficient MFT parsing
    from .progress import mft_progress
    # Estimate MFT record count from the MFT domain log if available,
    # otherwise fall back to a rough guess. Don't use file_size — for large
    # images it vastly overestimates (e.g. 18M vs 159K actual records).
    max_possible = None
    if state.mft_coverage_pct > 0 and state.total_mft_entries > 0:
        max_possible = state.total_mft_entries
    else:
        # Check for MFT domain log to estimate size
        basename = partition_basename(state.partition) if state.partition else ""
        domain_path = Path(state.image).parent / f"{basename}_mft_domain.log"
        if domain_path.exists():
            try:
                from .mapfile.parser import parse_mapfile_from_path
                domain = parse_mapfile_from_path(str(domain_path))
                mft_bytes = sum(e.size for e in domain.entries if e.status == '+')
                max_possible = mft_bytes // record_size
            except Exception:
                pass

    try:
        with open(image, 'rb') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                with mft_progress(max_possible) as update:
                    def _cb(idx, rec):
                        update(1)
                    records = iter_mft_records(mm, mft_offset, record_size,
                                               progress_callback=_cb)
            finally:
                mm.close()
    except (OSError, ValueError) as e:
        # Fallback: read the MFT region into memory
        try:
            with open(image, 'rb') as f:
                f.seek(0)
                data = f.read()
            records = iter_mft_records(data, mft_offset, record_size)
        except Exception as e2:
            print(f"  Error reading MFT: {e2}")
            return None

    # Save actual record count for future progress bar accuracy
    if records:
        state.total_mft_entries = max(r.index for r in records) + 1

    return build_tree(records, include_system=include_system, include_deleted=include_deleted)


def _do_recover(
    selected: set[int],
    tree,
    state: SessionState,
    work_dir: Path,
    output_dir: Path,
    retry: int = 0,
    dry_run: bool = False,
    ddrescue_extra: list[str] | None = None,
    bitlocker_password: str | None = None,
    bitlocker_recovery_key: str | None = None,
) -> int:
    """Run recovery (gddrescue) then extraction."""
    from .bootstrap import mount_bitlocker_fuse, unmount_bitlocker_fuse

    mapfile = None
    mf_path = Path(state.mapfile)
    if mf_path.exists():
        mapfile = parse_mapfile_from_path(str(mf_path))

    if mapfile is None:
        print("  Warning: No mapfile found. Skipping targeted recovery.")
    else:
        # For BitLocker volumes, FUSE-mount to get a decrypted source device
        fuse_mountpoint = None
        recovery_device = state.device
        if state.bitlocker and state.bitlocker_partition:
            pw, rk = bitlocker_password, bitlocker_recovery_key
            if not pw and not rk:
                from .bootstrap import prompt_bitlocker_credentials
                pw, rk = prompt_bitlocker_credentials()
            if not pw and not rk:
                print("  Error: BitLocker credentials required for recovery.")
                return 1

            fuse_mountpoint = work_dir / "_dislocker_fuse"
            fuse_file = mount_bitlocker_fuse(
                state.bitlocker_partition, fuse_mountpoint,
                password=pw, recovery_key=rk,
            )
            if fuse_file is None:
                print("  Error: BitLocker FUSE mount failed.")
                return 1
            recovery_device = fuse_file

        try:
            plan = plan_recovery(
                selected, tree, mapfile,
                state.cluster_size, state.partition_offset, work_dir,
            )
            print_plan(plan)

            if plan.bytes_to_read > 0 and recovery_device:
                if not dry_run:
                    try:
                        proceed = input("  Start disk recovery? [Y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return 0
                    if proceed == 'n':
                        return 0

                success = run_recovery(
                    plan, recovery_device, state.image, state.mapfile,
                    retry=retry, dry_run=dry_run,
                    ddrescue_extra=ddrescue_extra or [],
                )
                if not success and not dry_run:
                    print("  Warning: gddrescue reported errors.")

                # Reload mapfile after recovery
                if not dry_run and mf_path.exists():
                    mapfile = parse_mapfile_from_path(str(mf_path))

                # Show per-file results
                if mapfile:
                    results = assess_results(
                        selected, tree, mapfile,
                        state.cluster_size, state.partition_offset,
                    )
                    print_results(results)
        finally:
            if fuse_mountpoint:
                unmount_bitlocker_fuse(fuse_mountpoint)

    # Extract files
    if not dry_run:
        print(f"\n  Extracting files to {output_dir}...")
        ext_results = extract_selected(
            selected, tree, state.image, output_dir,
            state.cluster_size, state.partition_offset, mapfile,
        )
        write_report(ext_results, output_dir)

        complete = sum(1 for r in ext_results if r.complete)
        print(f"\n  Done. {complete}/{len(ext_results)} files recovered successfully.")
    else:
        print("  [dry-run] Skipping extraction.")

    return 0


def _build_ddrescue_extra(args) -> list[str]:
    """Collect ddrescue tuning flags from CLI args into a list of extra arguments."""
    extra: list[str] = []
    if getattr(args, 'no_trim', False):
        extra.append('--no-trim')
    if getattr(args, 'no_scrape', False):
        extra.append('-n')
    if getattr(args, 'reverse', False):
        extra.append('-R')
    if getattr(args, 'ddrescue_timeout', None) is not None:
        extra.extend(['-T', f'{args.ddrescue_timeout}s'])
    if getattr(args, 'min_read_rate', None):
        extra.extend(['-a', args.min_read_rate])
    # Freeform passthrough — split on whitespace
    raw = getattr(args, 'ddrescue_opts', '') or ''
    if raw.strip():
        import shlex
        extra.extend(shlex.split(raw))
    return extra


def _h(nbytes: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != 'B' else f"{nbytes} B"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} PB"


if __name__ == "__main__":
    sys.exit(main())
