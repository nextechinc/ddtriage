# ddtriage

NTFS-aware selective data recovery tool for Linux. Recover specific files and folders from failing or BitLocker-encrypted NTFS drives without imaging the entire disk.

> **Disclaimer:** This software is provided as-is, with no warranty of any kind. Data recovery is inherently risky -- there is no guarantee that any data can or will be recovered. The authors are not responsible for any data loss, damage, or other consequences resulting from the use or misuse of this tool. **Use at your own risk.** Always work from a copy or image when possible, and never write to a failing drive.

## What it does

Most disk recovery tools take an all-or-nothing approach: image the entire drive first, then figure out what you need. On a failing drive with bad sectors, that can take days and may never complete.

ddtriage works differently:

1. **Bootstrap** -- Recovers only the NTFS Master File Table (MFT) from the failing drive. The MFT is a small index (~100-200 MB) that describes every file and folder on the disk.
2. **Browse** -- Parses the MFT and presents an interactive file browser showing the complete directory tree, file sizes, and health indicators -- all without touching the failing drive again.
3. **Select** -- You pick exactly which files and folders you need. ddtriage calculates which disk sectors contain those files.
4. **Recover** -- Uses GNU ddrescue to surgically read only the sectors needed for your selected files, skipping everything else.
5. **Extract** -- Reads the recovered data from the image and writes your files to an output directory, preserving the original directory structure and timestamps.

The key principle: **the failing drive is never read twice for the same sector.** All reads go through GNU ddrescue and are cached in a progressive disk image. If recovery is interrupted, it resumes where it left off.

## Features

- **Selective recovery** -- Browse the full directory tree and pick individual files or folders
- **Interactive TUI** -- ncdu-style terminal browser with health indicators, search, and keyboard navigation
- **BitLocker support** -- Transparently decrypts BitLocker-encrypted volumes via FUSE (requires password or recovery key)
- **Progressive imaging** -- All disk reads are cached; subsequent operations reuse existing data
- **Health indicators** -- Per-file coverage status shows what's already in the image vs. what still needs to be read
- **MFT coverage reporting** -- Shows what percentage of the file table was recovered, so you know if the listing is incomplete
- **Compressed file support** -- Handles NTFS LZNT1 compression
- **Resilient parsing** -- Gracefully handles damaged MFT records, corrupt attributes, and partial data
- **ddrescue tuning** -- Expose advanced ddrescue options for difficult drives (reverse, no-trim, timeout, etc.)

## Requirements

### System packages

**Required:**

```bash
sudo apt install gddrescue python3-pip python3-venv
```

- `gddrescue` -- GNU ddrescue for low-level disk recovery (provides the `ddrescue` and `ddrescuelog` commands)
- Python 3.10 or later

**Recommended:**

```bash
sudo apt install ddrutility
```

- `ddrutility` -- Provides `ddru_ntfsbitmap`, which precisely identifies the MFT region on disk. If not installed, ddtriage falls back to its own boot sector parser with a size estimate.

**Optional (for BitLocker-encrypted drives):**

```bash
sudo apt install dislocker
# or as an alternative:
sudo apt install libbde-utils
```

- `dislocker` -- FUSE driver for reading BitLocker-encrypted volumes. ddtriage tries this first.
- `libbde-utils` -- Alternative BitLocker decryption tool (provides `bdemount`). Used as a fallback if dislocker fails.

> **Note:** Some distros (e.g. Ubuntu 24.04) ship older versions that can't handle newer BitLocker formats from Windows 10/11. If both tools fail, build dislocker from source:
> ```bash
> sudo apt install cmake gcc libfuse-dev libmbedtls-dev
> git clone https://github.com/Aorimn/dislocker.git
> cd dislocker && cmake . && make && sudo make install
> ```

### Python packages

The only Python dependency is [Textual](https://textual.textualize.io/) for the interactive terminal UI. It's installed automatically.

## Installation

### Using pipx (recommended)

[pipx](https://pipx.pypa.io/) installs Python tools in isolated environments while making them available system-wide.

```bash
# Install pipx if you don't have it
sudo apt install pipx

# Install ddtriage
pipx install git+https://github.com/nextechinc/ddtriage.git
```

Since `sudo` uses a restricted PATH that doesn't include `~/.local/bin`, use one of these to run with root privileges:

```bash
# Option A: pass your PATH to sudo
sudo env "PATH=$PATH" ddtriage /dev/sdb

# Option B: use the full path
sudo ~/.local/bin/ddtriage /dev/sdb

# Option C: add an alias to your shell profile
echo 'alias sudo-ddtriage="sudo env PATH=$PATH ddtriage"' >> ~/.bashrc
```

### From source (for development)

```bash
git clone https://github.com/nextechinc/ddtriage.git
cd ddtriage
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Run with:
sudo .venv/bin/ddtriage /dev/sdb
```

## Quick start

### Full interactive workflow

```bash
sudo ddtriage /dev/sdb
```

This walks you through disk selection, partition selection, MFT recovery, interactive browsing, file selection, targeted recovery, and extraction -- all in one session.

> **Note:** The examples below assume `ddtriage` is on your PATH (pipx install). If using a venv, substitute `sudo .venv/bin/ddtriage` instead.

### Step-by-step workflow

```bash
# 1. Bootstrap: recover the MFT from the failing drive
sudo ddtriage bootstrap /dev/sdb

# 2. Browse: interactive file browser (no disk I/O)
sudo ddtriage browse

# 3. Recover: read selected files from disk + extract
sudo ddtriage recover --selection selection.json --output ./recovered
```

### BitLocker-encrypted drives

```bash
# With recovery key (48-digit key from Windows setup)
sudo ddtriage --recovery-key 123456-789012-345678-901234-567890-123456-789012-345678 /dev/sdc

# With password
sudo ddtriage --bitlocker-password "mypassword" /dev/sdc
```

BitLocker decryption happens transparently via FUSE -- only the sectors you need are decrypted on demand, not the entire volume.

## Commands

### `ddtriage [device]`

Full interactive workflow. If a device path is provided, skips disk selection. Walks through all phases: bootstrap, browse, recover, extract.

### `ddtriage bootstrap [device]`

Recover the NTFS boot sector and MFT from the source device. Detects BitLocker encryption automatically. Creates a progressive disk image and ddrescue mapfile in the working directory.

Options:
- `--retry N` -- Number of ddrescue retry passes for bad sectors (default: 0, auto-set to 1 if errors occur)

### `ddtriage browse`

Launch the interactive TUI file browser. Reads from the existing disk image (no disk I/O). Select files and folders for recovery.

Options:
- `--show-deleted` -- Include deleted entries in the browser

### `ddtriage recover`

Run targeted disk recovery for previously selected files, then extract them.

Options:
- `--selection FILE` -- Path to selection JSON file (required)
- `--output DIR` -- Output directory for recovered files (default: `./recovered`)
- `--retry N` -- Number of ddrescue retry passes
- `--device DEV` -- Override source device from saved state

### `ddtriage extract`

Extract files from the existing disk image without additional disk reads. Useful when you've already recovered the data and want to re-extract.

Options:
- `--selection FILE` -- Path to selection JSON file (required)
- `--output DIR` -- Output directory (default: `./recovered`)

### `ddtriage scan`

Parse the MFT and report statistics (file count, directory count, orphans). No TUI, no disk I/O.

### `ddtriage tree`

Dump the directory tree to stdout in text format.

Options:
- `--show-deleted` -- Include deleted entries
- `--show-system` -- Include system metadata entries (MFT 0-15)

### `ddtriage info`

Display NTFS boot sector information: cluster size, MFT location, volume serial number, etc.

### `ddtriage status`

Display ddrescue mapfile coverage summary: total mapped, rescued, bad sectors.

## Global options

These options are available on all commands:

| Option | Description |
|--------|-------------|
| `--image PATH` | Path to disk image file (default: `<partition>.img`) |
| `--mapfile PATH` | Path to ddrescue mapfile (default: `<partition>.log`) |
| `--offset BYTES` | Partition offset in bytes (auto-detected) |
| `--output-dir DIR` | Working directory for all files (default: `./`) |
| `-v` | Increase verbosity (repeat for more: `-vv`) |
| `--dry-run` | Show what would be done without running ddrescue |

### BitLocker options

| Option | Description |
|--------|-------------|
| `--bitlocker-password PWD` | BitLocker user password |
| `--recovery-key KEY` | BitLocker 48-digit recovery key |

### ddrescue tuning options

For difficult drives, these options are passed through to every ddrescue invocation:

| Option | Description |
|--------|-------------|
| `--no-trim` | Skip trimming phase (faster first pass on very bad drives) |
| `--no-scrape` | Skip scraping phase |
| `--reverse` | Reverse direction of recovery passes |
| `--timeout SECS` | Max seconds since last successful read before giving up |
| `--min-read-rate RATE` | Minimum read rate (e.g. `1M`) before switching areas |
| `--ddrescue-opts "..."` | Any additional ddrescue flags passed through directly |

**Examples:**

```bash
# Drive that's failing from the beginning -- read backwards
sudo ddtriage --reverse /dev/sdb

# Very slow drive -- skip slow phases, give up after 60s of no progress
sudo ddtriage --no-trim --no-scrape --timeout 60 /dev/sdb

# Fine-grained control
sudo ddtriage --ddrescue-opts="-c 64 -K 100M --pause-on-error=5" /dev/sdb
```

## TUI browser

The interactive file browser shows the reconstructed directory tree from the MFT:

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Toggle selection (select/deselect file or folder for recovery) |
| `Enter` | Expand/collapse folder |
| `Right arrow` | Expand folder (or move to first child if already expanded) |
| `Left arrow` | Collapse folder (or move to parent) |
| `Up/Down arrows` | Navigate the tree |
| `/` | Search by filename |
| `a` | Select all files |
| `u` | Unselect all |
| `r` | Start recovery of selected files |
| `s` | Save current selection to JSON |
| `l` | Load selection from JSON |
| `q` | Quit |

### Health indicators

Each file shows a color-coded health indicator based on how much of its data is already in the disk image:

| Indicator | Color | Meaning |
|-----------|-------|---------|
| `██` | Green | 100% in image (or resident in MFT) |
| `▓▓ 73%` | Yellow | Partially in image |
| `░░` | Red | Not yet read from disk |
| `??` | Gray | No data (empty/sparse file) |

### File flags

| Flag | Meaning |
|------|---------|
| `[C]` | NTFS-compressed file |
| `[E]` | Encrypted file (EFS) |
| `[DEL]` | Deleted file (shown with `--show-deleted`) |

## How it works

### Architecture

```
Phase 1: Bootstrap       -->  Recover boot sector + MFT via ddrescue
Phase 2: MFT parse       -->  Parse MFT from image, build directory tree (no disk I/O)
Phase 3: TUI browser     -->  Interactive file browser with health indicators (no disk I/O)
Phase 4: Targeted recovery -->  Generate domain log for selected files, run ddrescue (surgical disk I/O)
Phase 5: File extraction  -->  Read file data from image, write to output directory (no disk I/O)
```

### Progressive disk image

All disk reads go through GNU ddrescue and land in a cumulative image file (e.g. `sdb1.img`) tracked by a mapfile (`sdb1.log`). The image is a sparse file that grows as regions are read. The mapfile records what's been rescued, what failed, and what's untried.

Key properties:
- The failing drive is never read twice for the same sector
- Recovery can be interrupted and resumed at any point
- Multiple recovery passes accumulate in the same image
- The mapfile is the source of truth for recovery state

### NTFS parsing

ddtriage includes a complete NTFS parser written from scratch for maximum resilience against damaged data:

- **Boot sector parser** -- Reads the NTFS BPB to find cluster size, MFT location, and record size
- **MFT parser** -- Parses MFT records with update sequence (fixup) array validation, gracefully skipping damaged entries
- **Attribute parser** -- Handles `$STANDARD_INFORMATION`, `$FILE_NAME`, `$DATA`, `$ATTRIBUTE_LIST`, and other NTFS attributes
- **Data run decoder** -- Decodes the compressed LCN/VCN cluster mapping that describes where file data lives on disk
- **Directory tree builder** -- Resolves parent references to reconstruct the full directory hierarchy, with orphan handling
- **LZNT1 decompressor** -- Handles NTFS-compressed files
- **Extension record merging** -- Handles `$ATTRIBUTE_LIST` for files with data spread across multiple MFT records

No external NTFS libraries are used. The parser is designed to gracefully handle missing bytes, partial records, and corrupted attributes without crashing.

### Targeted recovery

When you select files for recovery, ddtriage:

1. Collects the data run mappings for each selected file
2. Converts cluster runs to absolute byte ranges
3. Merges overlapping/adjacent ranges
4. Diffs against the mapfile to exclude already-rescued sectors
5. Generates a ddrescue domain logfile covering only the needed ranges
6. Runs ddrescue with the domain log, reading only the required sectors
7. Extracts file content from the image, preserving directory structure and timestamps

### BitLocker handling

For BitLocker-encrypted drives, ddtriage uses `dislocker-fuse` to create a transparent FUSE mount that decrypts data on demand. The FUSE-mounted virtual device replaces the raw device for all ddrescue operations, so the surgical targeted recovery approach is fully preserved -- only the sectors you need are decrypted, not the entire volume.

## Files created

ddtriage creates files in the working directory named after the selected partition:

| File | Description |
|------|-------------|
| `sdb1.img` | Progressive raw disk image (sparse file) |
| `sdb1.log` | GNU ddrescue mapfile tracking recovery state |
| `sdb1_mft_domain.log` | Domain log restricting reads to the MFT region |
| `ddtriage_state.json` | Session state (device, offsets, settings) |
| `selection.json` | Saved file selection for batch recovery |
| `recovery_domain.log` | Domain log for targeted file recovery |
| `sdb1-recovered/` | Output directory for extracted files |
| `sdb1-recovered/recovery_report.txt` | Detailed extraction report |

For BitLocker volumes, additional files are created with a `_decrypted` suffix (e.g. `sdc3_decrypted.img`).

## Tips for difficult recoveries

- **Start with `--no-trim`** on very bad drives. The trimming phase makes many small reads that can stress a failing drive.
- **Use `--reverse`** if the drive seems to fail progressively from the beginning. Reading backwards may reach good data faster.
- **Set `--timeout 60`** to skip areas where the drive stops responding entirely.
- **Run multiple passes.** ddtriage never re-reads rescued sectors. Each pass fills in more gaps.
- **Keep the drive cool.** Failing drives often work better when cold. Take breaks between passes.
- **Prioritize.** Browse the file tree first and recover your most important files before attempting large directories.

## Development

```bash
# Run tests
python -m pytest tests/ -v

# Run with verbose logging
sudo ddtriage -vv /dev/sdb
```

## License

MIT License. See [LICENSE](LICENSE) for details.

This tool calls external programs (GNU ddrescue, ddrutility, dislocker) via subprocess. These tools have their own licenses (GPL-2/GPL-3) but are not bundled or linked -- they are invoked as separate processes, the same as running them from a shell script.
