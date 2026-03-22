"""Progress bar utilities using Rich (bundled with Textual)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

try:
    from rich.progress import (
        Progress, BarColumn, TextColumn, TimeRemainingColumn,
        TaskProgressColumn, SpinnerColumn, MofNCompleteColumn,
    )
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


@contextmanager
def mft_progress(total: int | None = None) -> Generator:
    """Context manager for MFT parsing progress bar.

    Yields a callable(advance=1) to update progress.
    """
    if not HAS_RICH or total is None:
        count = [0]

        def _update(advance: int = 1) -> None:
            count[0] += advance
            if count[0] % 10000 == 0:
                print(f"  Parsed {count[0]} MFT records...", end='\r')

        yield _update
        if count[0] > 0:
            print(f"  Parsed {count[0]} MFT records.      ")
        return

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]Parsing MFT"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        task = progress.add_task("mft", total=total)

        def _update(advance: int = 1) -> None:
            progress.advance(task, advance)

        yield _update


@contextmanager
def extraction_progress(total: int) -> Generator:
    """Context manager for file extraction progress bar.

    Yields a callable(advance=1) to update progress.
    """
    if not HAS_RICH:
        count = [0]

        def _update(advance: int = 1) -> None:
            count[0] += advance

        yield _update
        return

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Extracting files"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        task = progress.add_task("extract", total=total)

        def _update(advance: int = 1) -> None:
            progress.advance(task, advance)

        yield _update


@contextmanager
def generic_progress(description: str, total: int | None = None) -> Generator:
    """Generic progress bar.

    Yields a callable(advance=1) to update progress.
    """
    if not HAS_RICH:
        yield lambda advance=1: None
        return

    progress = Progress(
        SpinnerColumn(),
        TextColumn(f"[bold]{description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        task = progress.add_task(description, total=total)

        def _update(advance: int = 1) -> None:
            progress.advance(task, advance)

        yield _update
