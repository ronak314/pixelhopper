"""Forward error correction helpers for chunked PNG storage.

This module owns Reed-Solomon style parity generation and recovery. The PNG
encoder only writes/reads PNG payloads; it does not implement FEC.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import zfec

from chunk_reader import read_file_in_chunks

DEFAULT_PARITY_CHUNKS = 20
PNG_KIND = Literal["data", "parity"]


@dataclass(slots=True)
class ShareRecord:
    """Metadata for one stored data or parity share."""

    index: int
    kind: PNG_KIND
    filename: str
    byte_length: int
    sha256: str


@dataclass(slots=True)
class StoragePlan:
    """Encoding plan and payloads for the PNG storage layer."""

    original_filename: str
    original_size: int
    chunk_size: int
    data_chunks: int
    parity_chunks: int
    total_chunks: int
    original_sha256: str
    chunk_hashes: list[dict[str, object]]
    share_records: list[ShareRecord]
    share_payloads: list[bytes]
    manifest_filename: str
    manifest: dict[str, object]


def sha256_bytes(data: bytes) -> str:
    """Return the SHA256 hex digest for bytes."""

    return hashlib.sha256(data).hexdigest()


def _chunk_name(original_name: str, index: int, kind: PNG_KIND) -> str:
    """Build a flat, unique filename for a share."""

    return f"{original_name}_chunk_{index:06d}_{kind}.png"


def _pad_block(block: bytes, block_size: int) -> bytes:
    """Pad a data block to the fixed block size used by zfec."""

    if block_size <= 0:
        return b""
    if len(block) >= block_size:
        return block
    return block + (b"\x00" * (block_size - len(block)))


def build_storage_plan(
    input_path: str | Path,
    chunk_size: int,
    parity_chunks: int = DEFAULT_PARITY_CHUNKS,
) -> StoragePlan:
    """Split a file into chunks, generate parity, and build a manifest."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if parity_chunks <= 0:
        raise ValueError("parity_chunks must be positive")

    input_file = Path(input_path)
    original_name = input_file.name
    original_hasher = hashlib.sha256()

    data_blocks: list[bytes] = []
    data_lengths: list[int] = []
    original_size = 0

    for block in read_file_in_chunks(input_file, chunk_size):
        data_blocks.append(block)
        data_lengths.append(len(block))
        original_size += len(block)
        original_hasher.update(block)

    if not data_blocks:
        data_blocks = [b""]
        data_lengths = [0]

    block_size = chunk_size if original_size > 0 else 0
    padded_blocks = [_pad_block(block, block_size) for block in data_blocks]

    data_chunk_count = len(data_blocks)
    total_chunks = data_chunk_count + parity_chunks

    encoder = zfec.Encoder(data_chunk_count, total_chunks)
    shares = encoder.encode(padded_blocks)

    share_records: list[ShareRecord] = []
    share_payloads: list[bytes] = []
    chunk_hashes: list[dict[str, object]] = []

    for share_index, share_payload in enumerate(shares):
        kind: PNG_KIND = "data" if share_index < data_chunk_count else "parity"
        display_index = share_index + 1
        filename = _chunk_name(original_name, display_index, kind)
        share_sha256 = sha256_bytes(share_payload)
        record = ShareRecord(
            index=display_index,
            kind=kind,
            filename=filename,
            byte_length=len(share_payload),
            sha256=share_sha256,
        )
        share_records.append(record)
        share_payloads.append(share_payload)
        chunk_hashes.append(
            {
                "index": display_index,
                "kind": kind,
                "filename": filename,
                "byte_length": len(share_payload),
                "sha256": share_sha256,
            }
        )

    manifest_filename = f"{original_name}_manifest.json"
    manifest = {
        "original_filename": original_name,
        "original_size": original_size,
        "chunk_size": chunk_size,
        "data_chunks": data_chunk_count,
        "parity_chunks": parity_chunks,
        "total_chunks": total_chunks,
        "original_sha256": original_hasher.hexdigest(),
        "chunk_hashes": chunk_hashes,
        "data_lengths": data_lengths,
        "manifest_filename": manifest_filename,
    }

    return StoragePlan(
        original_filename=original_name,
        original_size=original_size,
        chunk_size=chunk_size,
        data_chunks=data_chunk_count,
        parity_chunks=parity_chunks,
        total_chunks=total_chunks,
        original_sha256=original_hasher.hexdigest(),
        chunk_hashes=chunk_hashes,
        share_records=share_records,
        share_payloads=share_payloads,
        manifest_filename=manifest_filename,
        manifest=manifest,
    )


def recover_original_bytes(
    manifest: dict[str, object],
    available_shares: list[tuple[int, bytes]],
) -> bytes:
    """Recover the original bytes from the available verified shares."""

    data_chunks = int(manifest["data_chunks"])
    parity_chunks = int(manifest["parity_chunks"])
    total_chunks = int(manifest["total_chunks"])
    original_size = int(manifest["original_size"])
    original_sha256 = str(manifest["original_sha256"])

    if len(available_shares) < data_chunks:
        raise ValueError("not enough valid shares to reconstruct the file")

    decoder = zfec.Decoder(data_chunks, total_chunks)
    selected_blocks = [payload for _, payload in available_shares[:data_chunks]]
    selected_share_nums = [share_num for share_num, _ in available_shares[:data_chunks]]
    recovered_blocks = decoder.decode(selected_blocks, selected_share_nums)

    recovered = b"".join(recovered_blocks)[:original_size]
    if sha256_bytes(recovered) != original_sha256:
        raise ValueError("reconstructed file checksum does not match manifest")

    return recovered
