"""Encode and decode binary files as manageable square PNG chunk sets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, PngImagePlugin

from fec_layer import DEFAULT_PARITY_CHUNKS, build_storage_plan, recover_original_bytes

Chunk_MB = 50

DEFAULT_CHUNK_SIZE = Chunk_MB * 1024 * 1024
DEFAULT_ENCODED_ROOT = Path(__file__).resolve().parent / "encoded_png"
DEFAULT_RECONSTRUCTED_ROOT = Path(__file__).resolve().parent / "reconstructed"
DEFAULT_UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads"


@dataclass(slots=True)
class ChunkRecord:
    """Metadata for a single PNG chunk."""

    index: int
    filename: str
    byte_length: int
    image_width: int
    image_height: int
    padding_bytes: int
    sha256: str


@dataclass(slots=True)
class EncodingResult:
    """Summary of a chunked PNG encoding run."""

    original_path: Path
    output_dir: Path
    manifest_path: Path
    chunk_paths: list[Path]
    chunks: list[ChunkRecord]


def sha256_bytes(data: bytes) -> str:
    """Return the SHA256 hex digest for a bytes object."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return the SHA256 hex digest for a file without loading it all at once."""

    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_to_rgb_array(data: bytes, width: int | None = None) -> np.ndarray:
    """Convert raw bytes into a square 3-channel NumPy image array."""

    if width is not None and width <= 0:
        raise ValueError("width must be positive")

    pixel_count = math.ceil(len(data) / 3) if data else 1
    if width is None:
        width = max(1, math.ceil(math.sqrt(pixel_count)))

    padded_size = width * width * 3

    buffer = np.frombuffer(data, dtype=np.uint8)
    if buffer.size < padded_size:
        buffer = np.pad(buffer, (0, padded_size - buffer.size), mode="constant")

    return buffer.reshape((width, width, 3))


def _write_payload_png(payload: bytes, output_path: Path, kind: str) -> ChunkRecord:
    """Write arbitrary payload bytes to a square PNG and return its metadata."""

    image_array = bytes_to_rgb_array(payload)
    image_side = int(image_array.shape[0])
    padding_bytes = image_side * image_side * 3 - len(payload)

    image = Image.fromarray(image_array, mode="RGB")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("payload_length", str(len(payload)))
    metadata.add_text("payload_sha256", sha256_bytes(payload))
    metadata.add_text("payload_kind", kind)
    image.save(output_path, format="PNG", pnginfo=metadata)

    return ChunkRecord(
        index=0,
        filename=output_path.name,
        byte_length=len(payload),
        image_width=image_side,
        image_height=image_side,
        padding_bytes=padding_bytes,
        sha256=sha256_bytes(payload),
    )


def _chunk_name(original_name: str, index: int, *, kind: str | None = None, manifest: bool = False) -> str:
    """Build a flat, descriptive filename for a PNG output."""

    if manifest:
        return f"{original_name}_manifest.json"
    suffix = f"_{kind}" if kind else ""
    return f"{original_name}_chunk_{index:06d}{suffix}.png"


def _manifest_png_name(original_name: str) -> str:
    """Return the manifest JSON filename used for the storage set."""

    return _chunk_name(original_name, 1, manifest=True)


def _chunk_record_from_payload(payload: bytes, filename: str, index: int) -> ChunkRecord:
    """Build chunk metadata from a payload without writing it yet."""

    image_side = max(1, math.ceil(math.sqrt(max(1, math.ceil(len(payload) / 3)))))
    padding_bytes = image_side * image_side * 3 - len(payload)
    return ChunkRecord(
        index=index,
        filename=filename,
        byte_length=len(payload),
        image_width=image_side,
        image_height=image_side,
        padding_bytes=padding_bytes,
        sha256=sha256_bytes(payload),
    )


def encode_file_to_png(
    input_path: str | Path,
    output_dir: str | Path | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> EncodingResult:
    """Encode a binary file into a flat set of PNG data and parity chunks."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    input_file = Path(input_path)
    output_root = Path(output_dir) if output_dir is not None else DEFAULT_ENCODED_ROOT
    output_root.mkdir(parents=True, exist_ok=True)

    chunk_paths: list[Path] = []
    storage_plan = build_storage_plan(input_file, chunk_size=chunk_size, parity_chunks=DEFAULT_PARITY_CHUNKS)

    manifest_path = output_root / storage_plan.manifest_filename
    manifest_path.write_text(json.dumps(storage_plan.manifest, indent=2), encoding="utf-8")
    chunk_paths.append(manifest_path)

    for share_record, share_payload in zip(storage_plan.share_records, storage_plan.share_payloads):
        chunk_path = output_root / share_record.filename
        _write_payload_png(share_payload, chunk_path, kind=share_record.kind)
        chunk_paths.append(chunk_path)

    return EncodingResult(
        original_path=input_file,
        output_dir=output_root,
        manifest_path=manifest_path,
        chunk_paths=chunk_paths,
        chunks=[_chunk_record_from_payload(payload, record.filename, record.index) for record, payload in zip(storage_plan.share_records, storage_plan.share_payloads)],
    )


def resolve_manifest_json_path(input_path: str | Path) -> Path:
    """Resolve a decode input to its manifest JSON path.

    Accepts either the manifest JSON itself, an encoded output directory, or a
    source-like file path such as ``uploads/hello.txt``.
    """

    input_file = Path(input_path)

    if input_file.is_dir():
        manifest_candidates = sorted(input_file.glob("*_manifest.json"))
        if manifest_candidates:
            return manifest_candidates[0]

    if input_file.is_file() and input_file.name.endswith("_manifest.json"):
        return input_file

    candidate = DEFAULT_ENCODED_ROOT / f"{input_file.name}_manifest.json"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(f"manifest JSON not found for {input_path}")


def resolve_manifest_png_path(input_path: str | Path) -> Path:
    """Backward-compatible alias for the JSON manifest resolver."""

    return resolve_manifest_json_path(input_path)


def _load_manifest(input_path: str | Path) -> tuple[Path, dict[str, object]]:
    manifest_path = resolve_manifest_json_path(input_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest_path.parent, manifest


def decode_png_to_bytes(input_path: str | Path) -> bytes:
    """Reconstruct the original binary data from an encoded PNG chunk set."""

    encoded_dir, manifest = _load_manifest(input_path)
    chunk_entries = sorted(manifest["chunk_hashes"], key=lambda entry: entry["index"])  # type: ignore[index]

    available_shares: list[tuple[int, bytes]] = []
    for entry in chunk_entries:
        chunk_path = encoded_dir / entry["filename"]  # type: ignore[index]
        if not chunk_path.exists():
            continue
        try:
            with Image.open(chunk_path) as image:
                pixel_data = np.asarray(image.convert("RGB"), dtype=np.uint8).reshape(-1)
        except Exception:
            continue

        chunk_bytes = pixel_data.tobytes()
        expected_length = int(entry["byte_length"])
        if len(chunk_bytes) < expected_length:
            continue

        chunk_payload = chunk_bytes[:expected_length]
        if sha256_bytes(chunk_payload) != entry["sha256"]:
            continue

        available_shares.append((int(entry["index"]) - 1, chunk_payload))

    return recover_original_bytes(manifest, available_shares)


def decode_png_to_file(input_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Decode an encoded PNG chunk set back into a binary file."""

    resolved_output = Path(output_path) if output_path is not None else DEFAULT_RECONSTRUCTED_ROOT / decoded_filename_from_manifest(input_path)
    output_file = resolved_output
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_bytes(decode_png_to_bytes(input_path))
    return output_file


def decoded_filename_from_manifest(input_path: str | Path) -> str:
    """Return the original filename stored in the manifest."""

    _, manifest = _load_manifest(input_path)
    return Path(str(manifest["original_filename"])).name


def verify_file_round_trip(original_path: str | Path, reconstructed_path: str | Path) -> bool:
    """Compare two files by SHA256 checksum."""

    return sha256_file(original_path) == sha256_file(reconstructed_path)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the chunked encoder and decoder."""

    parser = argparse.ArgumentParser(description="Encode binary files into chunked PNG sets and decode them back.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    encode_parser = subparsers.add_parser("encode", help="Encode a binary file into chunked PNG files.")
    encode_parser.add_argument("input_path", type=Path, help="Path to the binary input file")
    encode_parser.add_argument("-o", "--output-dir", type=Path, help="Output directory for chunk PNGs")
    encode_parser.add_argument(
        "--chunk-size-mb",
        type=int,
        default=max(1, math.ceil(DEFAULT_CHUNK_SIZE / (1024 * 1024))),
        help="Chunk size in megabytes",
    )

    decode_parser = subparsers.add_parser("decode", help="Decode a chunked PNG set back into a binary file.")
    decode_parser.add_argument("input_path", type=Path, help="Path to the encoded directory, manifest, or source file")
    decode_parser.add_argument("-o", "--output", type=Path, help="Output binary file path")

    return parser


def _encode_with_checksums(input_path: Path, output_dir: Path | None, chunk_size_mb: int) -> EncodingResult:
    chunk_size_bytes = chunk_size_mb * 1024 * 1024
    result = encode_file_to_png(input_path, output_dir=output_dir, chunk_size=chunk_size_bytes)
    print(f"encoded_dir: {result.output_dir}")
    print(f"manifest: {result.manifest_path}")
    print(f"original_sha256: {sha256_file(input_path)}")
    return result


def _print_progress_bar(current: int, total: int, label: str, name: str) -> None:
    """Render a one-line console progress bar."""

    bar_width = 28
    filled_width = int(bar_width * current / total) if total else bar_width
    empty_width = bar_width - filled_width
    percent = int((current / total) * 100) if total else 100
    bar = f"{'#' * filled_width}{'-' * empty_width}"
    print(f"\r{label} [{bar}] {percent:3d}% {current}/{total} {name}", end="", flush=True)
    if current >= total:
        print()


def batch_encode_uploads(chunk_size_mb: int | None = None) -> list[EncodingResult]:
    """Encode every file currently present in the uploads folder."""

    effective_chunk_size_mb = chunk_size_mb if chunk_size_mb is not None else max(1, math.ceil(DEFAULT_CHUNK_SIZE / (1024 * 1024)))
    results: list[EncodingResult] = []
    upload_files = sorted(path for path in DEFAULT_UPLOAD_ROOT.iterdir() if path.is_file())

    if not upload_files:
        print(f"No files found in {DEFAULT_UPLOAD_ROOT}")
        return results

    total_files = len(upload_files)
    for index, upload_file in enumerate(upload_files, start=1):
        if not upload_file.exists():
            _print_progress_bar(index, total_files, "Encoding", f"skipped {upload_file.name}")
            continue
        output_dir = DEFAULT_ENCODED_ROOT
        _print_progress_bar(index - 1, total_files, "Encoding", upload_file.name)
        results.append(_encode_with_checksums(upload_file, output_dir, effective_chunk_size_mb))
        _print_progress_bar(index, total_files, "Encoding", upload_file.name)

    return results


def _decode_with_checksums(input_path: Path, output_path: Path | None) -> Path:
    manifest_path = resolve_manifest_json_path(input_path)
    resolved_output = output_path or (DEFAULT_RECONSTRUCTED_ROOT / decoded_filename_from_manifest(manifest_path))
    decode_png_to_file(manifest_path, resolved_output)
    print(f"decoded: {resolved_output}")
    print(f"reconstructed_sha256: {sha256_file(resolved_output)}")
    return resolved_output


def batch_decode_encoded_png_sets() -> list[Path]:
    """Decode every manifest-backed PNG set under encoded_png."""

    manifest_paths = sorted(DEFAULT_ENCODED_ROOT.glob("*_manifest.json"))
    decoded_paths: list[Path] = []

    if not manifest_paths:
        print(f"No manifest JSON files found in {DEFAULT_ENCODED_ROOT}")
        return decoded_paths

    total_sets = len(manifest_paths)
    for index, manifest_path in enumerate(manifest_paths, start=1):
        output_path = DEFAULT_RECONSTRUCTED_ROOT / decoded_filename_from_manifest(manifest_path)
        _print_progress_bar(index - 1, total_sets, "Decoding", manifest_path.name)
        decoded_paths.append(_decode_with_checksums(manifest_path, output_path))
        _print_progress_bar(index, total_sets, "Decoding", manifest_path.name)

    return decoded_paths


def purge_output_directories() -> None:
    """Delete everything from the upload, encoded, and reconstructed folders."""

    for directory in (DEFAULT_UPLOAD_ROOT, DEFAULT_ENCODED_ROOT, DEFAULT_RECONSTRUCTED_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
        for entry in directory.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def interactive_main() -> int:
    """Run a small interactive menu when no CLI arguments are provided."""

    print("Pixel Hopper")
    print("1) Encode every file in uploads")
    print("2) Decode every encoded set in encoded_png")
    print("3) Purge uploads, encoded_png, and reconstructed")
    print("4) Quit")

    choice = input("Choose an action [1/2/3/4]: ").strip().lower()
    if choice in {"4", "q", "quit", "exit"}:
        return 0

    if choice in {"1", "encode", "e"}:
        batch_encode_uploads()
        return 0

    if choice in {"2", "decode", "d"}:
        batch_decode_encoded_png_sets()
        return 0

    if choice in {"3", "purge", "p"}:
        confirm = input("Type PURGE to delete all files in uploads, encoded_png, and reconstructed: ").strip()
        if confirm == "PURGE" or confirm == "purge":
            purge_output_directories()
            print("Purged uploads, encoded_png, and reconstructed.")
            return 0
        print("Purge cancelled.")
        return 1

    print("Unknown choice.")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""

    if argv is None and len(sys.argv) == 1:
        return interactive_main()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "encode":
        _encode_with_checksums(args.input_path, args.output_dir, args.chunk_size_mb)
        return 0

    if args.command == "decode":
        _decode_with_checksums(args.input_path, args.output)
        return 0

    raise RuntimeError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
