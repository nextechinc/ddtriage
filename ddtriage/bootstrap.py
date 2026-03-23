"""Phase 1: Bootstrap — disk selection, MFT recovery via ddru_ntfsbitmap + gddrescue."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .ntfs.boot_sector import parse_boot_sector, detect_bitlocker, BitLockerDetected
from .mapfile.parser import parse_mapfile_from_path
from .mapfile.query import coverage_percentage

log = logging.getLogger(__name__)

STATE_FILENAME = "ddtriage_state.json"

# Files/patterns created during a recovery session
_SESSION_FILES = [
    "_bootsec_domain.log", "_bootsec_decrypted_domain.log",
    "_backup_bs_domain.log", "recovery_domain.log",
    "selection.json", STATE_FILENAME,
    # ddrutility intermediates
    "__bitmapfile", "__bootsec", "__mftshort",
    "__bootsec.log", "__mftshort.log",
    "ntfsbitmap_rescue_report.log", "_part0__bitmapfile.log",
]
_SESSION_DIRS = ["_dislocker_fuse"]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DiskInfo:
    """Information about a physical disk."""
    path: str               # e.g. /dev/sdb
    model: str
    vendor: str
    size_bytes: int
    size_human: str
    transport: str          # e.g. usb, sata, nvme
    partitions: list[PartitionInfo] = field(default_factory=list)


@dataclass
class PartitionInfo:
    """Information about a partition on a disk."""
    path: str               # e.g. /dev/sdb1
    number: int
    start_bytes: int        # offset from start of disk
    size_bytes: int
    size_human: str
    fs_type: str            # e.g. ntfs, ext4
    label: str


@dataclass
class SessionState:
    """Persistent state for a recovery session."""
    device: str = ""
    partition: str = ""
    image: str = ""
    mapfile: str = ""
    partition_offset: int = 0
    cluster_size: int = 0
    bytes_per_sector: int = 0
    sectors_per_cluster: int = 0
    mft_start_lcn: int = 0
    mft_record_size: int = 0
    total_mft_entries: int = 0
    mft_coverage_pct: float = 0.0
    bitlocker: bool = False
    bitlocker_partition: str = ""  # original encrypted partition path (for re-mounting FUSE)
    bootstrap_complete: bool = False
    mft_parse_complete: bool = False
    phase: str = "init"

    def save(self, work_dir: Path) -> None:
        path = work_dir / STATE_FILENAME
        with open(path, 'w') as f:
            json.dump(asdict(self), f, indent=2)
        log.info("State saved to %s", path)

    @classmethod
    def load(cls, work_dir: Path) -> SessionState | None:
        path = work_dir / STATE_FILENAME
        if not path.exists():
            return None
        with open(path, 'r') as f:
            data = json.load(f)
        state = cls()
        for k, v in data.items():
            if hasattr(state, k):
                setattr(state, k, v)
        return state


# ---------------------------------------------------------------------------
# Disk / partition discovery
# ---------------------------------------------------------------------------

def partition_basename(path: str) -> str:
    """Extract the device base name from a partition path.

    Examples: /dev/sdc3 → sdc3, /dev/nvme0n1p2 → nvme0n1p2, /dev/sdb → sdb
    """
    return Path(path).name


def find_session_files(work_dir: Path) -> list[Path]:
    """Find all recovery session files in the working directory."""
    found: list[Path] = []

    # Fixed-name files
    for name in _SESSION_FILES:
        p = work_dir / name
        if p.exists():
            found.append(p)

    # Partition-named files (*.img, *.log, *_decrypted.*, *_domain.log, etc.)
    for p in work_dir.iterdir():
        if p.is_file() and p.suffix in ('.img', '.log') and p.name not in _SESSION_FILES:
            found.append(p)

    # Directories
    for name in _SESSION_DIRS:
        p = work_dir / name
        if p.exists() and p.is_dir():
            found.append(p)

    # Recovery output dirs (*-recovered/)
    for p in work_dir.iterdir():
        if p.is_dir() and p.name.endswith('-recovered'):
            found.append(p)

    return sorted(found)


def cleanup_session_files(work_dir: Path) -> int:
    """Delete all recovery session files from the working directory.

    Returns the number of files/directories removed.
    """
    removed = 0
    for p in find_session_files(work_dir):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed += 1
        except OSError as e:
            log.warning("Could not remove %s: %s", p, e)
    return removed


def _human_size(nbytes: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} PB"


def _read_sys(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def list_disks() -> list[DiskInfo]:
    """Discover block devices and their partitions using lsblk."""
    try:
        result = subprocess.run(
            ["lsblk", "-Jbp", "-o",
             "NAME,TYPE,SIZE,MODEL,VENDOR,TRAN,FSTYPE,LABEL,PARTTYPE"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        log.error("lsblk not found")
        return []

    if result.returncode != 0:
        log.error("lsblk failed: %s", result.stderr)
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Failed to parse lsblk output")
        return []

    disks: list[DiskInfo] = []

    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue

        size = int(dev.get("size") or 0)
        disk = DiskInfo(
            path=dev["name"],
            model=(dev.get("model") or "").strip(),
            vendor=(dev.get("vendor") or "").strip(),
            size_bytes=size,
            size_human=_human_size(size),
            transport=(dev.get("tran") or "unknown"),
        )

        for child in dev.get("children", []):
            if child.get("type") != "part":
                continue
            part_size = int(child.get("size") or 0)
            # Get partition start offset via sfdisk
            part_start = _get_partition_start(child["name"])
            disk.partitions.append(PartitionInfo(
                path=child["name"],
                number=_part_number(child["name"]),
                start_bytes=part_start,
                size_bytes=part_size,
                size_human=_human_size(part_size),
                fs_type=(child.get("fstype") or "unknown"),
                label=(child.get("label") or ""),
            ))

        disks.append(disk)

    return disks


def _part_number(path: str) -> int:
    """Extract partition number from device path like /dev/sdb1 → 1."""
    digits = ""
    for c in reversed(path):
        if c.isdigit():
            digits = c + digits
        else:
            break
    return int(digits) if digits else 0


def _get_partition_start(part_path: str) -> int:
    """Get partition start offset in bytes using sfdisk or /sys."""
    # Try /sys first (fastest)
    dev_name = Path(part_path).name
    sys_start = _read_sys(f"/sys/class/block/{dev_name}/start")
    if sys_start:
        try:
            # /sys reports in 512-byte sectors
            return int(sys_start) * 512
        except ValueError:
            pass

    # Fallback to sfdisk
    # Find parent disk
    parent = part_path.rstrip("0123456789")
    if parent.endswith("p"):
        parent = parent[:-1]  # handle nvme0n1p1 → nvme0n1
    try:
        result = subprocess.run(
            ["sfdisk", "--json", parent],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            sfd = json.loads(result.stdout)
            for part in sfd.get("partitiontable", {}).get("partitions", []):
                if part.get("node") == part_path:
                    return int(part["start"]) * int(sfd["partitiontable"].get("sectorsize", 512))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return 0


# ---------------------------------------------------------------------------
# Interactive disk / partition selection
# ---------------------------------------------------------------------------

def select_disk_interactive(disks: list[DiskInfo]) -> DiskInfo | None:
    """Present disk list and let user choose."""
    if not disks:
        print("No disks found.")
        return None

    print("\n  Available disks:\n")
    for i, d in enumerate(disks, 1):
        vendor_model = f"{d.vendor} {d.model}".strip() or "Unknown"
        print(f"    {i})  {d.path}  —  {vendor_model}  [{d.size_human}]  ({d.transport})")
        if d.partitions:
            for p in d.partitions:
                label = f'  "{p.label}"' if p.label else ""
                print(f"           └─ {p.path}  {p.fs_type:<8} {p.size_human}{label}")
        else:
            print(f"           └─ (no partitions)")
    print()

    while True:
        try:
            choice = input("  Select disk [1-{}]: ".format(len(disks))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice:
            continue
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(disks):
                return disks[idx]
        except ValueError:
            pass
        print(f"  Invalid choice. Enter a number between 1 and {len(disks)}.")


def select_partition_interactive(disk: DiskInfo) -> PartitionInfo | None:
    """Present partition list for a disk and let user choose."""
    ntfs_parts = [p for p in disk.partitions if p.fs_type.lower() == "ntfs"]
    all_parts = disk.partitions

    if not all_parts:
        print(f"\n  No partitions found on {disk.path}.")
        return None

    # Show all partitions, highlight NTFS ones
    print(f"\n  Partitions on {disk.path}:\n")
    for i, p in enumerate(all_parts, 1):
        label = f'  "{p.label}"' if p.label else ""
        ntfs_marker = " ← NTFS" if p.fs_type.lower() == "ntfs" else ""
        print(f"    {i})  {p.path}  —  {p.fs_type:<8} {p.size_human}{label}{ntfs_marker}")
    print()

    if not ntfs_parts:
        print("  Warning: no NTFS partitions detected on this disk.")

    while True:
        try:
            choice = input("  Select partition [1-{}]: ".format(len(all_parts))).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice:
            continue
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(all_parts):
                selected = all_parts[idx]
                if selected.fs_type.lower() not in ("ntfs", "bitlocker"):
                    try:
                        confirm = input(
                            f"  {selected.path} is {selected.fs_type}, not NTFS. "
                            "Continue anyway? [y/N]: "
                        ).strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return None
                    if confirm != 'y':
                        continue
                return selected
        except ValueError:
            pass
        print(f"  Invalid choice. Enter a number between 1 and {len(all_parts)}.")


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def check_dependencies() -> list[str]:
    """Check that required external tools are installed. Returns list of missing tools."""
    missing = []
    for tool in ("ddrescue", "ddrescuelog", "ddru_ntfsbitmap"):
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


# ---------------------------------------------------------------------------
# BitLocker support
# ---------------------------------------------------------------------------

def check_bitlocker(image_path: Path, partition_offset: int) -> bool:
    """Check if the partition in the image is BitLocker-encrypted."""
    try:
        with open(image_path, 'rb') as f:
            f.seek(partition_offset)
            data = f.read(512)
        return detect_bitlocker(data)
    except OSError:
        return False


def prompt_bitlocker_credentials() -> tuple[str | None, str | None]:
    """Interactively prompt for BitLocker password or recovery key.

    Returns (password, recovery_key) — one will be set, the other None.
    """
    print("\n  This volume is BitLocker-encrypted.")
    print("  You need either the password or the 48-digit recovery key.\n")
    print("    1)  Enter password")
    print("    2)  Enter recovery key")
    print("    3)  Cancel\n")

    while True:
        try:
            choice = input("  Choice [1-3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, None

        if choice == '1':
            try:
                import getpass
                pw = getpass.getpass("  BitLocker password: ")
                if pw:
                    return pw, None
            except (EOFError, KeyboardInterrupt):
                print()
                return None, None
        elif choice == '2':
            try:
                key = input("  Recovery key (e.g. 123456-789012-...): ").strip()
                if key:
                    return None, key
            except (EOFError, KeyboardInterrupt):
                print()
                return None, None
        elif choice == '3':
            return None, None
        else:
            print("  Invalid choice.")


def mount_bitlocker_fuse(
    device: str,
    mountpoint: Path,
    password: str | None = None,
    recovery_key: str | None = None,
) -> str | None:
    """FUSE-mount a BitLocker-encrypted partition for transparent decryption.

    Tries dislocker-fuse first, falls back to bdemount if dislocker fails
    or isn't installed. Returns the path to the decrypted virtual file,
    or None on failure.
    """
    mountpoint.mkdir(parents=True, exist_ok=True)

    # Try dislocker-fuse first
    result = _try_dislocker(device, mountpoint, password, recovery_key)
    if result is not None:
        return result

    # Try bdemount as fallback
    result = _try_bdemount(device, mountpoint, password, recovery_key)
    if result is not None:
        return result

    # Both failed — give actionable guidance
    print()
    print("  Neither dislocker-fuse nor bdemount could decrypt this volume.")
    print("  This is usually caused by outdated versions that don't support")
    print("  newer BitLocker formats (Windows 10/11).")
    print()
    print("  To fix, build dislocker from source:")
    print("    sudo apt install cmake gcc libfuse-dev libmbedtls-dev")
    print("    git clone https://github.com/Aorimn/dislocker.git")
    print("    cd dislocker && cmake . && make && sudo make install")
    print()
    return None


def _try_dislocker(
    device: str,
    mountpoint: Path,
    password: str | None,
    recovery_key: str | None,
) -> str | None:
    """Try mounting with dislocker-fuse. Returns decrypted file path or None."""
    if shutil.which("dislocker-fuse") is None:
        log.info("dislocker-fuse not found, skipping")
        return None

    cmd = ["dislocker-fuse", "-r", "-s", "-V", device]

    if recovery_key:
        cmd.append(f"-p{recovery_key}")
    elif password:
        cmd.append(f"-u{password}")
    else:
        return None

    cmd.extend(["--", str(mountpoint)])

    log.info("Running: %s", " ".join(cmd))
    print("  Trying dislocker-fuse...")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr).strip()
        log.error("dislocker-fuse failed (exit %d): %s", result.returncode, output)
        if "password" in output.lower() or "key" in output.lower():
            print("  Error: incorrect password or recovery key.")
            return None  # Don't try bdemount — credentials are wrong
        print(f"  dislocker-fuse failed: {output or '(unknown error)'}")
        return None

    fuse_file = mountpoint / "dislocker-file"
    if not fuse_file.exists():
        unmount_bitlocker_fuse(mountpoint)
        return None

    print("  BitLocker FUSE mount active (dislocker).")
    return str(fuse_file)


def _try_bdemount(
    device: str,
    mountpoint: Path,
    password: str | None,
    recovery_key: str | None,
) -> str | None:
    """Try mounting with bdemount (libbde). Returns decrypted file path or None."""
    if shutil.which("bdemount") is None:
        log.info("bdemount not found, skipping")
        return None

    cmd = ["bdemount"]

    if recovery_key:
        cmd.extend(["-r", recovery_key])
    elif password:
        cmd.extend(["-p", password])
    else:
        return None

    cmd.extend([device, str(mountpoint)])

    log.info("Running: %s", " ".join(cmd))
    print("  Trying bdemount...")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr).strip()
        log.error("bdemount failed (exit %d): %s", result.returncode, output)
        if "password" in output.lower() or "key" in output.lower():
            print("  Error: incorrect password or recovery key.")
        else:
            print(f"  bdemount failed: {output or '(unknown error)'}")
        return None

    # bdemount exposes the volume as bde1
    bde_file = mountpoint / "bde1"
    if not bde_file.exists():
        unmount_bitlocker_fuse(mountpoint)
        return None

    print("  BitLocker FUSE mount active (bdemount).")
    return str(bde_file)


def unmount_bitlocker_fuse(mountpoint: Path) -> None:
    """Unmount a BitLocker FUSE mount (dislocker or bdemount)."""
    try:
        subprocess.run(["fusermount", "-u", str(mountpoint)],
                       capture_output=True, timeout=10)
        log.info("Unmounted FUSE at %s", mountpoint)
    except Exception as e:
        log.warning("Failed to unmount FUSE at %s: %s", mountpoint, e)


# ---------------------------------------------------------------------------
# MFT coverage
# ---------------------------------------------------------------------------

def _compute_mft_coverage(
    mapfile_path: Path,
    mft_domain_path: Path | None,
) -> float:
    """Compute what percentage of the MFT domain was successfully rescued.

    Compares the mapfile (what ddrescue actually recovered) against the
    MFT domain log (what we asked it to recover).

    Returns coverage as a percentage (0.0 – 100.0).
    """
    if not mapfile_path.exists():
        return 0.0
    if mft_domain_path is None or not mft_domain_path.exists():
        return 0.0

    try:
        mapfile = parse_mapfile_from_path(str(mapfile_path))
        domain = parse_mapfile_from_path(str(mft_domain_path))
    except Exception as e:
        log.warning("Could not compute MFT coverage: %s", e)
        return 0.0

    # The domain log has '+' entries for the regions we wanted
    total_bytes = 0
    rescued_bytes = 0
    for entry in domain.entries:
        if entry.status == '+':
            total_bytes += entry.size
            pct = coverage_percentage(mapfile, entry.pos, entry.size)
            rescued_bytes += int(entry.size * pct / 100.0)

    if total_bytes == 0:
        return 0.0

    result = (rescued_bytes / total_bytes) * 100.0
    if result >= 100.0:
        print(f"  MFT coverage: 100.0% ({_human_size(total_bytes)} recovered)")
    else:
        print(f"  MFT coverage: {result:.1f}% of {_human_size(total_bytes)}")
        if result < 90.0:
            print(f"  WARNING: Significant MFT gaps — file listing will be incomplete.")
        else:
            print(f"  Note: Some MFT records may be missing. File listing may be incomplete.")

    return min(result, 100.0)


# ---------------------------------------------------------------------------
# Bootstrap operations
# ---------------------------------------------------------------------------

def _run(cmd: list[str], desc: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with logging."""
    log.info("Running: %s", " ".join(cmd))
    print(f"  {desc}...")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        log.error("%s failed (exit %d): %s", cmd[0], result.returncode, result.stderr)
    return result


def run_bootstrap(
    partition: PartitionInfo,
    work_dir: Path,
    retry: int = 0,
    skip_ddru: bool = False,
    bitlocker_password: str | None = None,
    bitlocker_recovery_key: str | None = None,
    ddrescue_extra: list[str] | None = None,
) -> SessionState:
    """Run the full bootstrap sequence.

    1. Run ddru_ntfsbitmap to get MFT domain log
    2. Run gddrescue to recover the MFT
    3. Parse boot sector from the image (detect BitLocker, decrypt if needed)
    4. Save state

    Args:
        partition: The selected partition.
        work_dir: Working directory for image/log/temp files.
        retry: Number of gddrescue retry passes (-r flag).
        skip_ddru: Skip ddru_ntfsbitmap (for testing or when it's unavailable).
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # Determine parent disk device
    disk_device = partition.path.rstrip("0123456789")
    if disk_device.endswith("p"):
        disk_device = disk_device[:-1]

    extra = ddrescue_extra or []
    basename = partition_basename(partition.path)
    image_path = work_dir / f"{basename}.img"
    mapfile_path = work_dir / f"{basename}.log"
    mft_domain_path = work_dir / f"{basename}_mft_domain.log"
    used_domain_path = work_dir / f"{basename}_used_domain.log"

    state = SessionState(
        device=disk_device,
        partition=partition.path,
        image=str(image_path),
        mapfile=str(mapfile_path),
        partition_offset=partition.start_bytes,
        phase="bootstrap",
    )

    # Step 1: Recover boot sector to detect BitLocker vs plain NTFS
    offset = partition.start_bytes
    bs_domain = work_dir / "_bootsec_domain.log"
    with open(bs_domain, 'w') as f:
        f.write("# Boot sector domain\n")
        f.write("0x00000000  ?\n")
        f.write(f"0x{offset:08X}  0x{512:08X}  +\n")
    cmd = ["ddrescue", "-d", "-m", str(bs_domain)] + extra + [
           disk_device, str(image_path), str(mapfile_path)]
    result = _run(cmd, "Recovering boot sector")

    # Step 1b: Check for BitLocker — if encrypted, FUSE-mount for transparent
    # decryption so all subsequent reads go through the decrypted view
    fuse_mountpoint = None
    source_device = disk_device       # what ddrescue reads from
    source_offset = partition.start_bytes  # partition offset for ddru_ntfsbitmap

    if result.returncode != 0:
        log.warning("Boot sector recovery failed (exit %d)", result.returncode)

    if image_path.exists() and check_bitlocker(image_path, partition.start_bytes):
        state.bitlocker = True
        state.bitlocker_partition = partition.path
        print("  BitLocker-encrypted volume detected.")

        pw, rk = bitlocker_password, bitlocker_recovery_key
        if not pw and not rk:
            pw, rk = prompt_bitlocker_credentials()
        if not pw and not rk:
            print("  Error: BitLocker credentials required. Aborting.")
            state.save(work_dir)
            return state

        fuse_mountpoint = work_dir / "_dislocker_fuse"
        fuse_file = mount_bitlocker_fuse(
            partition.path, fuse_mountpoint, password=pw, recovery_key=rk,
        )
        if fuse_file is None:
            print("  Error: BitLocker FUSE mount failed. Aborting.")
            state.save(work_dir)
            return state

        # All subsequent reads go through the FUSE-decrypted file.
        # dislocker exposes bare NTFS (no partition table), so offset=0.
        # We need a FRESH image/mapfile since FUSE offsets start at 0
        # (incompatible with the raw disk image we started with).
        source_device = fuse_file
        source_offset = 0

        image_path = work_dir / f"{basename}_decrypted.img"
        mapfile_path = work_dir / f"{basename}_decrypted.log"
        state.image = str(image_path)
        state.mapfile = str(mapfile_path)
        state.partition_offset = 0

        # Recover the decrypted boot sector into the new image
        bs_domain2 = work_dir / "_bootsec_decrypted_domain.log"
        with open(bs_domain2, 'w') as f:
            f.write("# Decrypted boot sector domain\n")
            f.write("0x00000000  ?\n")
            f.write(f"0x{0:08X}  0x{512:08X}  +\n")
        cmd = ["ddrescue", "-d", "-m", str(bs_domain2)] + extra + [
               source_device, str(image_path), str(mapfile_path)]
        _run(cmd, "Recovering decrypted boot sector")

    # Step 2: Run ddru_ntfsbitmap
    ddru_ok = False
    if not skip_ddru and shutil.which("ddru_ntfsbitmap"):
        cmd = [
            "ddru_ntfsbitmap",
            "--mftdomain", str(mft_domain_path),
        ]
        if source_offset > 0:
            cmd.extend(["-i", str(source_offset)])
        cmd.extend([source_device, str(used_domain_path)])

        result = _run(cmd, "Analyzing NTFS structure with ddru_ntfsbitmap")
        ddru_ok = result.returncode == 0
        if not ddru_ok:
            print("  ddru_ntfsbitmap failed; falling back to manual bootstrap.")
    else:
        if not skip_ddru:
            print("  ddru_ntfsbitmap not found; falling back to manual bootstrap.")

    # Step 3: Recover MFT via gddrescue
    mft_domain_used = None
    if ddru_ok and mft_domain_path.exists():
        mft_domain_used = mft_domain_path
        cmd = ["ddrescue", "-d", "-m", str(mft_domain_path)] + extra + [
               source_device, str(image_path), str(mapfile_path)]
        result = _run(cmd, "Recovering MFT region")
        if result.returncode != 0:
            log.warning("gddrescue first pass returned exit code %d (partial recovery)",
                        result.returncode)
            print(f"  Warning: gddrescue first pass had errors (exit {result.returncode}).")
            print("  This is normal for failing drives — retrying bad sectors...")
            retry = max(retry, 1)
    else:
        try:
            _bootstrap_manual(partition, source_device, work_dir, state, ddrescue_extra=extra)
        except BitLockerDetected:
            print("  Error: This volume is BitLocker-encrypted.")
            print("  Re-run with --recovery-key or --bitlocker-password to decrypt.")
            state.save(work_dir)
            return state
        except RuntimeError as e:
            print(f"  Error: {e}")
            state.save(work_dir)
            return state
        if mft_domain_path.exists():
            mft_domain_used = mft_domain_path

    # Step 3b: Retry pass (always at least 1 if first pass had errors)
    if retry > 0:
        domain_flag = ["-m", str(mft_domain_used)] if mft_domain_used else []
        cmd = ["ddrescue", "-d", f"-r{retry}"] + domain_flag + extra + [
            source_device, str(image_path), str(mapfile_path)]
        _run(cmd, f"Retry pass ({retry} retries)")

    # Step 3c: Compute MFT coverage percentage
    state.mft_coverage_pct = _compute_mft_coverage(
        mapfile_path, mft_domain_used,
    )

    # Unmount FUSE if it was used
    if fuse_mountpoint:
        unmount_bitlocker_fuse(fuse_mountpoint)

    # Step 4: Parse boot sector from image
    img_to_parse = Path(state.image)
    parse_offset = state.partition_offset
    if img_to_parse.exists():
        try:
            with open(img_to_parse, 'rb') as f:
                f.seek(parse_offset)
                bs_data = f.read(512)
            bs = parse_boot_sector(bs_data)
            state.bytes_per_sector = bs.bytes_per_sector
            state.sectors_per_cluster = bs.sectors_per_cluster
            state.cluster_size = bs.cluster_size
            state.mft_start_lcn = bs.mft_start_lcn
            state.mft_record_size = bs.mft_record_size
            print(f"  Boot sector parsed: cluster_size={bs.cluster_size}, "
                  f"MFT at LCN {bs.mft_start_lcn}, record_size={bs.mft_record_size}")
        except BitLockerDetected:
            print("  Error: image is still encrypted after decryption attempt.")
        except Exception as e:
            log.warning("Could not parse boot sector: %s", e)
            print(f"  Warning: could not parse boot sector: {e}")

    state.bootstrap_complete = True
    state.phase = "mft_parse"
    state.save(work_dir)
    print("  Bootstrap complete.")

    return state


def _bootstrap_manual(
    partition: PartitionInfo,
    source_device: str,
    work_dir: Path,
    state: SessionState,
    ddrescue_extra: list[str] | None = None,
) -> None:
    """Fallback bootstrap: recover boot sector, parse it, then recover MFT."""
    extra = ddrescue_extra or []
    basename = partition_basename(partition.path)
    image_path = Path(state.image) if state.image else work_dir / f"{basename}.img"
    mapfile_path = Path(state.mapfile) if state.mapfile else work_dir / f"{basename}.log"
    mft_domain_path = work_dir / f"{basename}_mft_domain.log"

    # Use state offset (0 for BitLocker FUSE, partition_start for raw disk)
    offset = state.partition_offset

    # Recover boot sector (first sector of partition)
    bs_domain = work_dir / "_bootsec_domain.log"
    with open(bs_domain, 'w') as f:
        f.write("# Boot sector domain\n")
        f.write("0x00000000  ?\n")
        f.write(f"0x{offset:08X}  0x{512:08X}  +\n")

    cmd = ["ddrescue", "-d", "-m", str(bs_domain)] + extra + [
           source_device, str(image_path), str(mapfile_path)]
    result = _run(cmd, "Recovering boot sector")
    if result.returncode != 0:
        raise RuntimeError(f"Could not recover boot sector: {result.stderr}")

    # Parse boot sector
    with open(image_path, 'rb') as f:
        f.seek(offset)
        bs_data = f.read(512)

    try:
        bs = parse_boot_sector(bs_data)
    except BitLockerDetected:
        raise  # Let caller handle BitLocker detection
    except ValueError as primary_err:
        # Try backup boot sector (last sector of partition)
        if partition.size_bytes <= 512:
            raise RuntimeError(
                f"Primary boot sector unreadable ({primary_err}) and partition "
                f"size unknown — cannot locate backup boot sector."
            )

        print(f"  Primary boot sector unreadable ({primary_err}), trying backup...")
        backup_offset = offset + partition.size_bytes - 512
        backup_domain = work_dir / "_backup_bs_domain.log"
        with open(backup_domain, 'w') as f:
            f.write("# Backup boot sector domain\n")
            f.write("0x00000000  ?\n")
            f.write(f"0x{backup_offset:08X}  0x{512:08X}  +\n")

        cmd = ["ddrescue", "-d", "-m", str(backup_domain)] + extra + [
               source_device, str(image_path), str(mapfile_path)]
        result = _run(cmd, "Recovering backup boot sector")
        if result.returncode != 0:
            raise RuntimeError("Could not recover backup boot sector either.")

        with open(image_path, 'rb') as f:
            f.seek(backup_offset)
            bs_data = f.read(512)

        if len(bs_data) < 512:
            raise RuntimeError(
                f"Backup boot sector read returned {len(bs_data)} bytes "
                f"(expected 512) at offset 0x{backup_offset:X}."
            )

        bs = parse_boot_sector(bs_data)

    # Calculate MFT location and create domain log
    mft_byte_offset = bs.mft_start_lcn * bs.cluster_size + offset
    # Estimate MFT size: we don't know exactly, so recover a reasonable chunk.
    # Start with 64 MB (covers ~65536 MFT records at 1024 bytes each).
    # gddrescue will stop at EOF anyway.
    mft_size_estimate = 64 * 1024 * 1024

    with open(mft_domain_path, 'w') as f:
        f.write("# MFT domain (manual bootstrap)\n")
        f.write("0x00000000  ?\n")
        f.write(f"0x{mft_byte_offset:08X}  0x{mft_size_estimate:08X}  +\n")

    cmd = ["ddrescue", "-d", "-m", str(mft_domain_path)] + extra + [
           source_device, str(image_path), str(mapfile_path)]
    result = _run(cmd, "Recovering MFT region (manual)")
    if result.returncode != 0:
        raise RuntimeError(f"MFT recovery failed: {result.stderr}")
