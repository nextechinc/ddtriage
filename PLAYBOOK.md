# diskrescue — NTFS-Aware Selective Data Recovery Tool

## Project brief

Build a CLI/TUI Linux utility that recovers specific files and folders from failing NTFS drives. The tool works in phases: bootstrap critical disk structures with minimal reads, parse the NTFS MFT to build a browsable directory tree, let the user select exactly which files to recover via an interactive TUI, then perform surgical targeted reads of only the needed clusters via gddrescue, and finally extract recovered files to a destination directory.

The core design principle is a **progressive disk image model**: all disk reads go through gddrescue and land in a cumulative image file (`sdb.img`) tracked by a mapfile (`sdb.log`). The failing drive is never read twice for the same sector. Subsequent operations read from the image whenever possible, only touching the physical drive for blocks not yet in the image.

---

## Architecture overview

```
Phase 1: Bootstrap        →  Recover boot sector + MFT via ddru_ntfsbitmap
Phase 2: MFT parse        →  Parse MFT from sdb.img, build directory tree (zero disk I/O)
Phase 3: TUI browser      →  ncdu-style file browser with health indicators (zero disk I/O)
Phase 4: Targeted recovery →  Generate domain log for selected files, run gddrescue (surgical disk I/O)
Phase 5: File extraction   →  Read file data from sdb.img, write to output directory (zero disk I/O)
```

All phases share two files that persist across runs and interruptions:

- `./sdb.img` — the progressive raw disk image (sparse file, grows as regions are read)
- `./sdb.log` — the gddrescue mapfile tracking what's been read, what failed, and what's untried

---

## Project structure

```
diskrescue/
├── diskrescue/
│   ├── __init__.py
│   ├── cli.py                  # Main CLI entry point (argparse or click)
│   ├── bootstrap.py            # Phase 1: call ddru_ntfsbitmap, run initial gddrescue
│   ├── ntfs/
│   │   ├── __init__.py
│   │   ├── boot_sector.py      # Parse NTFS boot sector (BPB) from image
│   │   ├── mft_parser.py       # Parse MFT entries, decode attributes, build file records
│   │   ├── data_runs.py        # Decode NTFS data run encoding (LCN/VCN cluster mapping)
│   │   ├── attributes.py       # NTFS attribute types ($FILE_NAME, $DATA, $INDEX_ROOT, etc.)
│   │   └── tree.py             # Build directory tree from MFT parent references
│   ├── mapfile/
│   │   ├── __init__.py
│   │   ├── parser.py           # Parse gddrescue mapfile format
│   │   ├── query.py            # Query coverage: is byte range X:Y fully rescued?
│   │   └── generator.py        # Generate domain logfiles for targeted recovery
│   ├── recovery/
│   │   ├── __init__.py
│   │   ├── orchestrator.py     # Coordinate gddrescue calls, manage image/log state
│   │   └── extractor.py        # Read file content from image using data runs, write to disk
│   └── tui/
│       ├── __init__.py
│       └── browser.py          # Textual-based interactive file tree browser
├── tests/
│   ├── test_boot_sector.py
│   ├── test_mft_parser.py
│   ├── test_data_runs.py
│   ├── test_mapfile.py
│   └── test_tree.py
├── pyproject.toml
└── README.md
```

---

## Phase 1: Bootstrap (`bootstrap.py`)

### Goal

Recover the NTFS boot sector and MFT with minimal disk reads, using ddrutility's battle-tested `ddru_ntfsbitmap` as the bootstrap tool.

### Implementation

1. **Detect partition layout.** The source device may be a whole disk (`/dev/sdb`) or a partition (`/dev/sdb1`). Use `fdisk -l` or `sfdisk --json` to detect partitions. If a whole disk is provided, find the first NTFS partition and calculate `--inputoffset` (partition start in bytes). Store the offset for all subsequent operations.

2. **Run ddru_ntfsbitmap with --mftdomain.** This is a subprocess call:

```bash
ddru_ntfsbitmap --mftdomain ./mft_domain.log -i <partition_offset_bytes> /dev/sdX ./used_domain.log
```

This produces:
- `./mft_domain.log` — domain logfile scoped to just the MFT region
- `./used_domain.log` — domain logfile for all used clusters (we'll use this later)
- Several intermediate files (`__bootsec`, `__mftshort`, etc.)

3. **Recover the MFT via gddrescue** using the MFT domain:

```bash
ddrescue -d -m ./mft_domain.log /dev/sdX ./sdb.img ./sdb.log
```

The `-d` flag uses direct disk access (bypasses kernel cache, important for failing drives). The `-m` flag restricts reads to the MFT domain only.

4. **Retry pass for any MFT gaps** (optional, controlled by CLI flag):

```bash
ddrescue -d -r3 -m ./mft_domain.log /dev/sdX ./sdb.img ./sdb.log
```

5. **Validate MFT coverage.** After gddrescue completes, check `sdb.log` against `mft_domain.log` using `ddrescuelog --show-status` or our own mapfile parser. Report MFT recovery percentage to user. Warn if significant gaps exist.

### Dependencies

- `ddrutility` (specifically `ddru_ntfsbitmap`) — must be installed on the system
- `gddrescue` (specifically `ddrescue` and `ddrescuelog`) — must be installed

### Error handling

- If `ddru_ntfsbitmap` fails (corrupt boot sector, wrong offset), fall back to manual boot sector recovery: read sector 0 of the partition via gddrescue, parse it ourselves, calculate MFT location, and create the domain log manually.
- If the boot sector at the expected location is damaged, try the NTFS backup boot sector (last sector of the partition).
- Store the partition offset persistently (e.g., in a JSON state file alongside sdb.img) so subsequent tool invocations don't need to re-detect it.

---

## Phase 2: MFT Parser (`ntfs/`)

### Goal

Parse the MFT from `sdb.img` (no disk I/O) and build a complete directory tree with per-file data run mappings.

### Boot sector parsing (`boot_sector.py`)

Read bytes at `partition_offset` from `sdb.img`. Parse the NTFS BPB (BIOS Parameter Block):

| Offset | Size | Field                        |
|--------|------|------------------------------|
| 0x00   | 3    | Jump instruction             |
| 0x03   | 8    | OEM ID ("NTFS    ")          |
| 0x0B   | 2    | Bytes per sector             |
| 0x0D   | 1    | Sectors per cluster          |
| 0x28   | 8    | Total sectors                |
| 0x30   | 8    | MFT start LCN               |
| 0x38   | 8    | MFTMirr start LCN            |
| 0x40   | 1    | Clusters per MFT record *    |
| 0x44   | 1    | Clusters per index block *   |
| 0x48   | 8    | Volume serial number         |

\* If the value is negative, the record size is `2^|value|` bytes (e.g., -10 = 1024 bytes, which is the standard MFT record size).

Derived values:
- `cluster_size = bytes_per_sector * sectors_per_cluster`
- `mft_offset = mft_start_lcn * cluster_size + partition_offset`
- `record_size` = typically 1024 bytes

Validate: check OEM ID is "NTFS", bytes_per_sector is 512/1024/2048/4096, sectors_per_cluster is a power of 2.

### MFT entry parsing (`mft_parser.py`)

Each MFT record is 1024 bytes (standard). Structure:

| Offset | Size | Field                       |
|--------|------|-----------------------------|
| 0x00   | 4    | Signature ("FILE")          |
| 0x04   | 2    | Offset to update sequence   |
| 0x06   | 2    | Size of update sequence     |
| 0x10   | 2    | Sequence number             |
| 0x14   | 2    | Hard link count             |
| 0x16   | 2    | Offset to first attribute   |
| 0x18   | 2    | Flags (1=in use, 2=directory)|
| 0x1C   | 4    | Used size of MFT entry      |
| 0x20   | 4    | Allocated size              |
| 0x28   | 8    | Base record (for extensions) |

**Important**: Apply the update sequence (fixup) array before parsing attributes. The last 2 bytes of each sector within the record are replaced by the update sequence during write; they must be restored for correct parsing.

#### Fixup algorithm

1. Read the update sequence array starting at offset 0x04, length at 0x06.
2. First entry is the expected value. Subsequent entries are the original bytes.
3. For each sector in the record, verify the last 2 bytes match the expected value, then replace them with the corresponding original bytes from the array.

### Attribute parsing (`attributes.py`)

Attributes start at offset 0x16 (relative to record start) and are chained. Each attribute has a common header:

| Offset | Size | Field               |
|--------|------|---------------------|
| 0x00   | 4    | Attribute type      |
| 0x04   | 4    | Attribute length    |
| 0x08   | 1    | Non-resident flag   |
| 0x09   | 1    | Name length         |
| 0x0A   | 2    | Name offset         |
| 0x0C   | 2    | Flags               |
| 0x0E   | 2    | Attribute ID        |

Attribute type 0xFFFFFFFF marks end of attributes.

Key attribute types:

- **0x10 $STANDARD_INFORMATION** — timestamps, permissions. Resident.
- **0x30 $FILE_NAME** — file name (UTF-16LE), parent directory reference (MFT record number + sequence), file size, timestamps, filename namespace (0=POSIX, 1=Win32, 2=DOS, 3=Win32+DOS). Resident. A file may have multiple $FILE_NAME attributes (Win32 name + DOS 8.3 name). **Prefer namespace 1 (Win32) or 3 (Win32+DOS) for display; fall back to 2 (DOS) only if no Win32 name exists.**
- **0x40 $OBJECT_ID** — GUID. Resident.
- **0x80 $DATA** — file content. May be resident (small files, data stored inline in the attribute) or non-resident (data stored in clusters on disk, described by data runs).
- **0x90 $INDEX_ROOT** — directory index root. Resident.
- **0xA0 $INDEX_ALLOCATION** — directory index data. Non-resident.
- **0xB0 $BITMAP** — bitmap for index allocation. Can be partition-level ($Bitmap file, MFT entry 6) or per-directory.

#### Resident attribute data

For resident attributes, the data is inline:

| Offset | Size | Field          |
|--------|------|----------------|
| 0x10   | 4    | Data length    |
| 0x14   | 2    | Data offset    |

#### Non-resident attribute data

For non-resident attributes, the header extends:

| Offset | Size | Field                    |
|--------|------|--------------------------|
| 0x10   | 8    | Start VCN                |
| 0x18   | 8    | End VCN                  |
| 0x20   | 2    | Data runs offset         |
| 0x28   | 8    | Allocated size           |
| 0x30   | 8    | Real (actual) size       |
| 0x38   | 8    | Initialized size         |

The data runs (cluster mapping) start at offset 0x20 from the attribute start.

### Data run decoding (`data_runs.py`)

This is the core algorithm that maps file content to physical disk locations. Data runs are a compressed encoding of cluster extents.

Each run starts with a header byte where the low nibble is the number of bytes encoding the run length, and the high nibble is the number of bytes encoding the run offset (relative to previous run start).

```
header_byte = length_size | (offset_size << 4)
```

- Read `length_size` bytes as unsigned integer → run length in clusters
- Read `offset_size` bytes as **signed** integer → offset from previous LCN (or absolute for first run)
- If `offset_size` is 0, this is a sparse run (no physical clusters allocated)

Terminator: header byte of 0x00.

**Critical**: The offset is signed and relative. Accumulate a running LCN:

```python
current_lcn = 0
runs = []
while header_byte != 0:
    length = read_unsigned(length_size)
    if offset_size > 0:
        offset = read_signed(offset_size)
        current_lcn += offset
        runs.append((current_lcn, length))  # physical LCN, length in clusters
    else:
        runs.append((None, length))  # sparse run
```

To convert data runs to absolute byte offsets for gddrescue:

```python
for lcn, length in runs:
    if lcn is not None:
        byte_offset = (lcn * cluster_size) + partition_offset
        byte_length = length * cluster_size
        # This byte range maps to physical disk
```

### $FILE_NAME parent reference

The parent directory reference in $FILE_NAME is 8 bytes: the lower 6 bytes are the parent MFT record number, the upper 2 bytes are the sequence number. Extract:

```python
parent_record = reference & 0x0000FFFFFFFFFFFF
parent_seq = (reference >> 48) & 0xFFFF
```

### Directory tree construction (`tree.py`)

1. **First pass**: Iterate all MFT entries (0 to N). For each entry with the "in use" flag set:
   - Parse $FILE_NAME attribute(s), extract name, parent reference, file size
   - Parse $DATA attribute(s), extract data runs (non-resident) or resident data
   - Parse $STANDARD_INFORMATION for timestamps
   - Store as a FileRecord: `{ mft_index, name, parent_mft_index, is_directory, size, data_runs, timestamps, resident_data }`

2. **Second pass**: Build the tree by resolving parent references. Start from MFT entry 5 (root directory, "."). Recursively attach children to parents.

3. **Handle orphans**: Files whose parent MFT entry is not "in use" or whose parent reference is invalid get placed in a virtual `/ORPHANS/` directory.

4. **Handle attribute lists**: Large directories or heavily fragmented files may have an $ATTRIBUTE_LIST (type 0x20) that points to extension MFT records. The base record has a non-zero "base record" field at offset 0x28. Extension records should be merged into the base record's attributes.

### Special MFT entries

| Index | Name       | Purpose                         |
|-------|------------|---------------------------------|
| 0     | $MFT       | The MFT itself                  |
| 1     | $MFTMirr   | MFT mirror (first 4 entries)    |
| 2     | $LogFile   | NTFS journal                    |
| 3     | $Volume    | Volume information              |
| 4     | $AttrDef   | Attribute definitions           |
| 5     | .          | Root directory                  |
| 6     | $Bitmap    | Cluster allocation bitmap       |
| 7     | $Boot      | Boot sector                     |
| 8     | $BadClus   | Bad cluster list                |
| 9     | $Secure    | Security descriptors            |
| 10    | $UpCase    | Uppercase table                 |
| 11    | $Extend    | Extended metadata directory     |
| 12-15 | (reserved) |                                 |
| 16+   | User files | Normal files and directories    |

Skip entries 0-15 from the user-facing directory tree. They are system metadata.

### Data structures

```python
@dataclass
class FileRecord:
    mft_index: int
    name: str                           # Win32 preferred, DOS fallback
    parent_mft_index: int
    is_directory: bool
    is_deleted: bool                    # Flag bit 0 not set
    size: int                           # Real size from $DATA attribute
    data_runs: list[tuple[int, int]]    # [(lcn, cluster_count), ...] or None for resident
    resident_data: bytes | None         # Inline data for small files
    created: datetime
    modified: datetime
    children: list['FileRecord']        # Populated during tree build
    
@dataclass
class DirectoryTree:
    root: FileRecord                    # MFT entry 5
    orphans: list[FileRecord]           # Parentless files
    all_records: dict[int, FileRecord]  # mft_index → FileRecord
    total_files: int
    total_dirs: int
```

---

## Phase 3: Mapfile Operations (`mapfile/`)

### Goal

Parse, query, diff, and generate gddrescue mapfiles. This is the glue between the NTFS parser and gddrescue.

### Mapfile format (`parser.py`)

A gddrescue mapfile (log file) is a text file:

```
# Rescue Logfile. Created by GNU ddrescue version 1.27
# Command line: ddrescue -d /dev/sdb ./sdb.img ./sdb.log
# Start time:   2024-01-15 10:30:00
# current_pos  current_status
0x00180000     +
#      pos        size  status
0x00000000  0x00100000  +
0x00100000  0x00080000  *
0x00180000  0x7FFFFFFF  ?
```

- Lines starting with `#` are comments (preserve them).
- The first data line is the current position and status (for resuming).
- Subsequent lines are: `position  size  status`
- Status codes: `?` = non-tried, `*` = non-trimmed, `/` = non-scraped, `-` = bad sector, `+` = finished (rescued)

### Query operations (`query.py`)

Key queries we need:

- **`is_range_rescued(start, length) → bool`**: Returns True if the entire byte range [start, start+length) is covered by `+` (finished) entries.
- **`get_range_status(start, length) → list[RangeStatus]`**: Returns a breakdown of the range into sub-ranges with their status. Used for the health indicator in the TUI.
- **`coverage_percentage(start, length) → float`**: What percentage of this range is rescued?

### Domain log generation (`generator.py`)

Generate a domain logfile that restricts gddrescue to specific byte ranges:

```python
def generate_domain_log(byte_ranges: list[tuple[int, int]], output_path: str):
    """
    Write a gddrescue domain logfile.
    
    byte_ranges: list of (start_offset, length) tuples
    
    The domain log format is the same as a regular mapfile, but:
    - Ranges we WANT rescued are marked as '+' (finished/included in domain)
    - Ranges we DON'T want are marked as '?' (excluded from domain)
    - gddrescue's -m flag reads this and only processes '+' regions
    """
```

**Critical**: Before generating, diff the requested ranges against `sdb.log` to exclude ranges already rescued. This avoids re-reading sectors we already have:

```python
def generate_targeted_domain(
    file_data_runs: list[tuple[int, int]],  # byte offset, length
    existing_mapfile: str,                   # sdb.log path
    output_path: str                         # domain log output
) -> tuple[int, int]:                        # (bytes_already_rescued, bytes_to_read)
```

### Merging and consolidation

Adjacent or overlapping byte ranges from multiple selected files should be merged before generating the domain log. gddrescue is more efficient with contiguous ranges.

---

## Phase 4: TUI Browser (`tui/browser.py`)

### Goal

An interactive ncdu-style terminal UI for browsing the reconstructed directory tree and selecting files/folders for recovery.

### Framework

Use **Textual** (Python TUI framework). Install: `pip install textual`

### Features

1. **Tree view**: Directory tree with expand/collapse. Shows filename, size (human-readable), and modified date.

2. **Checkbox selection**: Space bar toggles selection on files and folders. Selecting a folder selects all children. Show selected count and total selected size in a footer bar.

3. **Health indicator**: For each file, show a color-coded status based on cross-referencing its data runs against `sdb.log`:
   - `██` Green: 100% of clusters already in image, or file is resident (data in MFT)
   - `▓▓` Yellow: Partially in image (show percentage)
   - `░░` Red: 0% in image (clusters not yet attempted)
   - `??` Gray: Unknown (sparse file, no data runs)

4. **Search**: `/` to search by filename. Filter the tree to matching entries.

5. **Info panel**: When a file is highlighted, show details in a side panel or bottom panel:
   - Full path
   - Size
   - Created / Modified timestamps
   - Data run count (fragmentation indicator)
   - Cluster coverage status
   - Whether data is resident in MFT

6. **Footer bar**: Shows:
   - Total files/dirs in tree
   - Number selected
   - Total size selected
   - Estimated bytes to read from disk (already-rescued clusters excluded)
   - Keybindings reminder

7. **Actions**:
   - `Enter` or `r`: Begin recovery of selected files (transitions to Phase 4/5)
   - `q`: Quit
   - `s`: Save selection to file (for batch/scripted recovery later)
   - `l`: Load selection from file

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ diskrescue - /dev/sdb → ./sdb.img                          │
├──────────────────────────────────────────┬──────────────────┤
│ [█] 📁 Users/                    2.1 GB │ Path: /Users/... │
│ [█]   📁 john/                   1.8 GB │ Size: 1.8 GB     │
│ [█]     📁 Documents/            800 MB │ Modified: ...     │
│ [ ]       📄 report.docx  ██    15 MB │ Clusters: 3 runs  │
│ [█]       📄 taxes.xlsx   ▓▓    2 MB  │ Coverage: 73%     │
│ [ ]     📁 Pictures/             1.0 GB │ Resident: No      │
│ [ ]   📁 admin/                  300 MB │                    │
├──────────────────────────────────────────┴──────────────────┤
│ Selected: 3 files, 817 MB | To read: 220 MB | [r]ecover    │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 5: Targeted Recovery & Extraction (`recovery/`)

### Recovery orchestration (`orchestrator.py`)

1. **Collect data runs** from all selected files. Convert cluster runs to absolute byte ranges using the partition offset and cluster size.

2. **Merge overlapping/adjacent ranges.** Sort by start offset, merge ranges that overlap or are within one cluster of each other.

3. **Diff against sdb.log.** Remove ranges already fully rescued. This is the key optimization — subsequent recovery passes get cheaper.

4. **Generate domain logfile.** Write to a temporary file (e.g., `./recovery_domain.log`).

5. **Report to user.** Before running gddrescue, show:
   - Total bytes to read from disk
   - Number of distinct ranges
   - Estimated time (based on drive speed if known, or just "depends on drive condition")

6. **Run gddrescue:**

```bash
ddrescue -d -m ./recovery_domain.log /dev/sdX ./sdb.img ./sdb.log
```

Same image and log files — gddrescue appends to both. The domain log restricts it to only the needed ranges.

7. **Optional retry pass:**

```bash
ddrescue -d -r3 -m ./recovery_domain.log /dev/sdX ./sdb.img ./sdb.log
```

8. **Report results.** Show per-file recovery status: which files are complete, which have gaps.

### File extraction (`extractor.py`)

1. **For each selected file**, read its content from `sdb.img` using the data runs:

```python
def extract_file(record: FileRecord, image_path: str, output_path: str, 
                 cluster_size: int, partition_offset: int, mapfile: Mapfile):
    """
    Read file content from the image using data runs.
    Write to output_path, preserving directory structure.
    Report gaps where clusters couldn't be read.
    """
    if record.resident_data is not None:
        # Small file — data is in MFT record itself
        write_bytes(output_path, record.resident_data[:record.size])
        return
    
    with open(image_path, 'rb') as img:
        output_bytes = bytearray()
        for lcn, cluster_count in record.data_runs:
            if lcn is None:
                # Sparse run — write zeros
                output_bytes.extend(b'\x00' * cluster_count * cluster_size)
                continue
            
            offset = (lcn * cluster_size) + partition_offset
            img.seek(offset)
            data = img.read(cluster_count * cluster_size)
            
            # Check mapfile — were these clusters actually rescued?
            if not mapfile.is_range_rescued(offset, cluster_count * cluster_size):
                # Log warning: partial data for this run
                pass
            
            output_bytes.extend(data)
        
        # Trim to actual file size (last cluster may be partial)
        write_bytes(output_path, bytes(output_bytes[:record.size]))
```

2. **Reconstruct directory structure.** For each selected file, compute its full path from root by walking parent references. Create intermediate directories in the output location.

3. **Preserve timestamps.** Set mtime/atime on extracted files using `os.utime()`.

4. **Generate recovery report.** Write a summary file listing:
   - Files successfully recovered (100% clusters read)
   - Files partially recovered (with byte ranges of gaps)
   - Files failed (MFT entry damaged, no data runs available)

---

## CLI interface (`cli.py`)

### Commands

```bash
# Full interactive workflow
diskrescue /dev/sdb

# Individual phases (for scripting or resuming)
diskrescue bootstrap /dev/sdb [--output-dir ./] [--retry 3]
diskrescue scan [--image ./sdb.img] [--offset <bytes>]
diskrescue browse [--image ./sdb.img] [--mapfile ./sdb.log]
diskrescue recover --selection ./selection.json --output ./recovered/ [--retry 3]
diskrescue extract --image ./sdb.img --selection ./selection.json --output ./recovered/

# Utilities
diskrescue info ./sdb.img          # Show boot sector info, MFT stats
diskrescue status ./sdb.log        # Show mapfile coverage summary
diskrescue tree ./sdb.img          # Dump directory tree to stdout (non-interactive)
```

### Global options

- `--image PATH` — path to the image file (default: `./sdb.img`)
- `--mapfile PATH` — path to the gddrescue mapfile (default: `./sdb.log`)
- `--offset BYTES` — partition offset in bytes (auto-detected if not provided)
- `--output-dir PATH` — working directory for image/log/temp files (default: `./`)
- `--verbose` / `-v` — increase verbosity
- `--dry-run` — show what would be done without executing gddrescue

### State persistence

Store session state in `./diskrescue_state.json`:

```json
{
    "device": "/dev/sdb",
    "image": "./sdb.img",
    "mapfile": "./sdb.log",
    "partition_offset": 1048576,
    "cluster_size": 4096,
    "bytes_per_sector": 512,
    "sectors_per_cluster": 8,
    "mft_start_lcn": 786432,
    "mft_record_size": 1024,
    "total_mft_entries": 0,
    "bootstrap_complete": false,
    "mft_parse_complete": false,
    "phase": "bootstrap"
}
```

---

## Testing strategy

### Test VM setup

The dev VM has `/dev/sdb` with an NTFS partition containing test files. To set up test scenarios:

```bash
# Create a test disk (if setting up from scratch)
# Assume /dev/sdb is a small disk or virtual disk

# Partition and format
sudo parted /dev/sdb mklabel gpt
sudo parted /dev/sdb mkpart primary ntfs 1MiB 100%
sudo mkfs.ntfs -f /dev/sdb1

# Mount and populate
sudo mkdir -p /mnt/testdisk
sudo mount /dev/sdb1 /mnt/testdisk

# Create test structure
sudo mkdir -p /mnt/testdisk/Documents
sudo mkdir -p /mnt/testdisk/Pictures
sudo mkdir -p /mnt/testdisk/Projects/code

# Various file sizes for testing
echo "Small resident file" | sudo tee /mnt/testdisk/tiny.txt                          # < 700 bytes, will be resident in MFT
dd if=/dev/urandom bs=1K count=50 | sudo tee /mnt/testdisk/Documents/medium.bin > /dev/null  # ~50KB
dd if=/dev/urandom bs=1M count=10 | sudo tee /mnt/testdisk/Pictures/large.bin > /dev/null    # ~10MB, likely fragmented
dd if=/dev/urandom bs=1M count=100 | sudo tee /mnt/testdisk/Projects/huge.bin > /dev/null    # ~100MB
sudo cp /usr/share/doc -r /mnt/testdisk/Documents/docs/                                      # Many small files in nested dirs

# Some files with special characters in names
sudo touch "/mnt/testdisk/Documents/report (final).docx"
sudo touch "/mnt/testdisk/Documents/résumé.pdf"

# Unmount cleanly
sudo umount /mnt/testdisk
```

### Unit tests

Write unit tests for each module independently using constructed/synthetic data:

- **test_boot_sector.py**: Construct a 512-byte boot sector with known values, verify parsing.
- **test_mft_parser.py**: Construct MFT records with known attributes, verify parsing and fixup.
- **test_data_runs.py**: Encode known cluster runs, verify decoding. Test edge cases: single run, many fragments, sparse runs, large LCN offsets, negative offsets.
- **test_mapfile.py**: Write a mapfile with known ranges, verify queries return correct coverage.
- **test_tree.py**: Build a tree from a set of FileRecords with known parent refs, verify structure.

### Integration tests

Against the actual test disk:

1. Run full bootstrap, verify `sdb.img` is created and MFT region is rescued.
2. Parse MFT, verify expected test files appear in tree with correct sizes.
3. Verify resident file (`tiny.txt`) data is readable from MFT.
4. Select specific files, generate domain log, verify byte ranges are correct.
5. Run full recovery, compare extracted files byte-for-byte against originals.

---

## Dependencies

### System packages (apt)

```bash
sudo apt install -y gddrescue ddrutility python3-pip python3-venv
```

If `ddrutility` is not in the repos, build from source:

```bash
git clone https://github.com/fwiessner/ddrutility.git
cd ddrutility
make
sudo make install
```

### Python packages

```
textual>=0.40.0        # TUI framework
click>=8.0             # CLI framework (or argparse if you prefer zero deps)
rich>=13.0             # Terminal formatting (bundled with Textual)
pytest>=7.0            # Testing
```

No external NTFS libraries — we're writing our own parser for maximum control over partial/damaged data handling. We need to gracefully handle missing bytes, partial MFT records, and corrupted attributes without crashing.

---

## Implementation order

Build in this order, testing each module before moving on:

### Sprint 1: Foundation

1. `mapfile/parser.py` + `mapfile/query.py` — parse and query mapfiles
2. `ntfs/boot_sector.py` — parse NTFS BPB
3. `ntfs/data_runs.py` — decode data run encoding
4. `ntfs/attributes.py` — parse attribute headers and key types
5. `ntfs/mft_parser.py` — parse MFT entries with fixup
6. Unit tests for all of the above

### Sprint 2: Tree + Bootstrap

7. `ntfs/tree.py` — build directory tree from parsed MFT
8. `bootstrap.py` — orchestrate ddru_ntfsbitmap + gddrescue
9. `mapfile/generator.py` — generate domain logfiles
10. Integration test: bootstrap against /dev/sdb, parse MFT, verify tree

### Sprint 3: TUI

11. `tui/browser.py` — interactive tree browser with Textual
12. Selection export/import (JSON)
13. Health indicators (cross-reference data runs vs mapfile)

### Sprint 4: Recovery + Extraction

14. `recovery/orchestrator.py` — generate targeted domain, run gddrescue
15. `recovery/extractor.py` — read files from image, write to output
16. `cli.py` — tie everything together with CLI commands
17. End-to-end integration test

### Sprint 5: Hardening

18. Error handling for corrupted MFT entries (graceful skip, log warning)
19. $ATTRIBUTE_LIST support (extension MFT records for large/fragmented files)
20. Deleted file display (optional flag to show deleted entries, greyed out)
21. Progress bars for long operations (MFT parsing, recovery)
22. Compressed file support (NTFS compression flag in $DATA attribute)

---

## Key references

- NTFS on-disk structure: https://flatcap.github.io/linux-ntfs/ntfs/index.html
- ddrutility source (MFT parsing, data runs): https://github.com/fwiessner/ddrutility
- gddrescue manual: https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html
- gddrescue mapfile format: https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html#Mapfile-structure
- NTFS data runs explained: https://www.file-recovery.com/recovery-define-clusters-chain-ntfs.htm

---

## Important design notes

1. **Never read from the physical drive except through gddrescue.** Every disk interaction must use gddrescue so it's logged in `sdb.log` and cached in `sdb.img`. This is the fundamental safety guarantee.

2. **Fail gracefully on partial data.** The MFT parser MUST handle gaps — some records will be unreadable. Skip damaged entries, log them, and continue. The tree builder should handle orphaned nodes.

3. **Image file should be sparse.** When creating `sdb.img`, use `fallocate` or `truncate` to create a sparse file matching the source disk size. gddrescue handles this automatically, but make sure no other code densifies it.

4. **All byte offsets are absolute to the image file.** When we say "partition offset," that's the byte offset within `sdb.img` (which is a raw image of the entire disk). The mapfile also uses absolute offsets. Keep this consistent everywhere — don't mix partition-relative and disk-absolute offsets.

5. **Thread safety is not needed.** This is a single-user CLI tool. Keep it simple — synchronous code, one thing at a time.

6. **The mapfile is the source of truth for recovery state.** Don't duplicate what's in `sdb.log`. Query it when you need to know what's been rescued.
