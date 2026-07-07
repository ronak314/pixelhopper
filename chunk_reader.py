"""Utilities for reading large files incrementally."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO


def read_file_in_chunks(path: str | Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield a file's contents in fixed-size chunks.

    This keeps memory usage bounded because only one chunk is held at a time.

    Args:
        path: Path to the file to read.
        chunk_size: Number of bytes to read per chunk. Must be positive.

    Yields:
        Bytes objects containing consecutive pieces of the file.
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    file_path = Path(path)
    with file_path.open("rb") as file_handle:
        yield from _iter_chunks(file_handle, chunk_size)


def _iter_chunks(file_handle: BinaryIO, chunk_size: int) -> Iterator[bytes]:
    while True:
        chunk = file_handle.read(chunk_size)
        if not chunk:
            break
        yield chunk
