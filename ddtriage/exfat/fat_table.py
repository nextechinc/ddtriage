"""Read and traverse the exFAT File Allocation Table."""

from __future__ import annotations

import struct
from .boot_sector import ExFATBootSector

EXFAT_EOC = 0xFFFFFFFF
EXFAT_BAD = 0xFFFFFFF7
EXFAT_FREE = 0x00000000


class ExFATTable:
    """Parsed exFAT File Allocation Table."""

    def __init__(self, data, bs: ExFATBootSector, partition_offset: int = 0):
        self.bs = bs
        fat_start = partition_offset + bs.fat_byte_offset
        fat_bytes = bs.fat_length * bs.bytes_per_sector
        fat_end = fat_start + fat_bytes

        if fat_end > len(data):
            self._fat_data = bytes(data[fat_start:len(data)])
        else:
            self._fat_data = bytes(data[fat_start:fat_end])

    def get_entry(self, cluster: int) -> int:
        """Read the FAT entry for a cluster. Full 32-bit entries in exFAT."""
        offset = cluster * 4
        if offset + 4 > len(self._fat_data):
            return EXFAT_EOC
        return struct.unpack_from('<I', self._fat_data, offset)[0]

    def follow_chain(self, start_cluster: int, max_clusters: int = 1_000_000) -> list[tuple[int, int]]:
        """Follow a cluster chain, merging consecutive clusters into runs.

        Returns list of (start_cluster, count) tuples.
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

            if next_entry >= EXFAT_EOC or next_entry == EXFAT_BAD or next_entry < 2:
                runs.append((run_start, run_count))
                break

            if next_entry == current + 1:
                run_count += 1
            else:
                runs.append((run_start, run_count))
                run_start = next_entry
                run_count = 1

            current = next_entry

        return runs

    def chain_to_data_runs(
        self, start_cluster: int, max_clusters: int = 1_000_000,
    ) -> list[tuple[int | None, int]]:
        """Follow chain and return (LCN, count) tuples compatible with the pipeline."""
        lcn_base = self.bs.heap_byte_offset // self.bs.cluster_size
        runs = self.follow_chain(start_cluster, max_clusters)
        return [(lcn_base + (c - 2), n) for c, n in runs]

    def contiguous_data_runs(
        self, start_cluster: int, cluster_count: int,
    ) -> list[tuple[int | None, int]]:
        """Return data runs for a contiguous file (NoFatChain flag set)."""
        lcn_base = self.bs.heap_byte_offset // self.bs.cluster_size
        return [(lcn_base + (start_cluster - 2), cluster_count)]
