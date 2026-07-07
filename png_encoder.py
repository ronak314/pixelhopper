"""Encode binary data as an RGB PNG image."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image


def bytes_to_rgb_array(data: bytes, width: int | None = None) -> np.ndarray:
    """Convert raw bytes into a 3-channel NumPy image array.

    Each pixel stores three consecutive bytes as RGB values. If the final pixel
    is incomplete, it is padded with zeros.

    Args:
        data: Raw binary data to encode.
        width: Optional image width in pixels. If omitted, a near-square width
            is chosen automatically.

    Returns:
        A uint8 array shaped as ``(height, width, 3)``.
    """

    if width is not None and width <= 0:
        raise ValueError("width must be positive")

    pixel_count = math.ceil(len(data) / 3) if data else 1
    if width is None:
        width = max(1, math.isqrt(pixel_count))
        while pixel_count % width != 0:
            width -= 1

    height = math.ceil(pixel_count / width)
    padded_size = height * width * 3

    buffer = np.frombuffer(data, dtype=np.uint8)
    if buffer.size < padded_size:
        buffer = np.pad(buffer, (0, padded_size - buffer.size), mode="constant")

    return buffer.reshape((height, width, 3))


def encode_bytes_to_png(data: bytes, output_path: str | Path, width: int | None = None) -> Path:
    """Write binary data to a PNG image and return the output path."""

    image_array = bytes_to_rgb_array(data, width=width)
    image = Image.fromarray(image_array, mode="RGB")

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_file, format="PNG")
    return output_file


def encode_file_to_png(input_path: str | Path, output_path: str | Path, width: int | None = None) -> Path:
    """Read a binary file and encode it as a PNG image."""

    input_file = Path(input_path)
    return encode_bytes_to_png(input_file.read_bytes(), output_path, width=width)
