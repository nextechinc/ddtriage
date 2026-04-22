"""Read and traverse the FAT (File Allocation Table)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .boot_sector import FATBootSector

# FAT32 special cluster values
FAT32_EOC = 0x0FFFFFF8      # end of chain (>= this value)
FAT32_BAD = 0x0FFFFFF7      # bad cluster
FAT32_FREE = 0x00000000     # free cluster

# FAT16 special values
FAT16_EOC = 0xFFF8
FAT16_BAD = 0xFFF7

# FAT12 special values
FAT12_EOC = 0xFF8
FAT12_BAD = 0xFF7


class FATTable:
    """Parsed FAT (File Allocation Table).

    Provides cluster chain traversal for file data recovery.
    """

    def __init__(self, data: bytes, bs: FATBootSector, partition_offset: int = 0):
        """Read the FAT from image data.

        Args:
            data: Raw image bytes (or mmap).
            bs: Parsed boot sector.
            partition_offset: Absolute byte offset of the partition.
        """
        self.bs = bs
        self.partition_offset = partition_offset

        fat_start = partition_offset + bs.fat_start
        fat_bytes = bs.fat_size * bs.bytes_per_sector
        fat_end = fat_start + fat_bytes

        if fat_end > len(data):
            # Read what we can
            self._fat_data = bytes(data[fat_start:len(data)])
        else:
            self._fat_data = bytes(data[fat_start:fat_end])

    def get_entry(self, cluster: int) -> int:
        """Read the FAT entry for the given cluster number."""
        if self.bs.fs_type == "FAT32":
            offset = cluster * 4
            if offset + 4 > len(self._fat_data):
                return FAT32_EOC
            return struct.unpack_from('<I', self._fat_data, offset)[0] & 0x0FFFFFFF
        elif self.bs.fs_type == "FAT16":
            offset = cluster * 2
            if offset + 2 > len(self._fat_data):
                return FAT16_EOC
            return struct.unpack_from('<H', self._fat_data, offset)[0]
        else:  # FAT12
            offset = cluster + (cluster // 2)
            if offset + 2 > len(self._fat_data):
                return FAT12_EOC
            val = struct.unpack_from('<H', self._fat_data, offset)[0]
            if cluster & 1:
                return val >> 4
            else:
                return val & 0x0FFF

    def is_eoc(self, entry: int) -> bool:
        """Check if a FAT entry marks end of chain."""
        if self.bs.fs_type == "FAT32":
            return entry >= FAT32_EOC
        elif self.bs.fs_type == "FAT16":
            return entry >= FAT16_EOC
        else:
            return entry >= FAT12_EOC

    def is_bad(self, entry: int) -> bool:
        """Check if a FAT entry marks a bad cluster."""
        if self.bs.fs_type == "FAT32":
            return entry == FAT32_BAD
        elif self.bs.fs_type == "FAT16":
            return entry == FAT16_BAD
        else:
            return entry == FAT12_BAD

    def follow_chain(self, start_cluster: int, max_clusters: int = 1_000_000) -> list[tuple[int, int]]:
        """Follow a cluster chain and return consolidated data runs.

        Returns list of (start_cluster, count) tuples, merging
        consecutive clusters into single runs.
        """
        if start_cluster < 2:
            return []

        runs: list[tuple[int, int]] = []
        current = start_cluster
        run_start = current
        run_count = 1
        visited = 0

        while visited < max_clusters:
            next_entry = self.get_entry(current)
            visited += 1

            if self.is_eoc(next_entry) or self.is_bad(next_entry) or next_entry < 2:
                runs.append((run_start, run_count))
                break

            if next_entry == current + 1:
                # Consecutive — extend current run
                run_count += 1
            else:
                # Non-consecutive — save current run, start new one
                runs.append((run_start, run_count))
                run_start = next_entry
                run_count = 1

            current = next_entry

        return runs

    def chain_to_data_runs(
        self, start_cluster: int, max_clusters: int = 1_000_000,
    ) -> list[tuple[int | None, int]]:
        """Follow a cluster chain and return data runs as (LCN, cluster_count).

        Converts FAT cluster numbers to LCN values that work with the
        recovery pipeline's byte offset calculation:
            byte_offset = LCN * cluster_size + partition_offset

        FAT cluster C maps to: data_start + (C - 2) * cluster_size
        So equivalent LCN = data_start // cluster_size + (C - 2)
        """
        lcn_base = self.bs.data_start // self.bs.cluster_size
        runs = self.follow_chain(start_cluster, max_clusters)
        return [(lcn_base + (c - 2), n) for c, n in runs]
