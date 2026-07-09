"""Flask interface for uploading files and monitoring processing status."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import tempfile
import zipfile

from flask import Flask, abort, after_this_request, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from png_encoder import (
    encode_file_to_png,
    decode_png_to_file,
    sha256_file,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
PNG_DIR = BASE_DIR / "encoded_png"
RECONSTRUCTED_DIR = BASE_DIR / "reconstructed"
DECODE_UPLOAD_DIR = BASE_DIR / "decode_uploads"


Status = Literal["queued", "processing", "complete", "failed"]


@dataclass
class Job:
    id: str
    filename: str
    status: Status = "queued"
    progress: int = 0
    message: str = "Queued"
    original_path: Path | None = None
    encoded_dir: Path | None = None
    manifest_path: Path | None = None
    reconstructed_path: Path | None = None
    original_sha256: str | None = None
    chunk_count: int = 0
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "id": self.id,
                "filename": self.filename,
                "status": self.status,
                "progress": self.progress,
                "message": self.message,
                "original_sha256": self.original_sha256,
                "chunk_count": self.chunk_count,
                "error": self.error,
                "encoded_dir": str(self.encoded_dir) if self.encoded_dir else None,
                "manifest_path": str(self.manifest_path) if self.manifest_path else None,
                "chunks": _chunk_list(self.id, self.encoded_dir) if self.encoded_dir else [],
            }

    def update(self, *, status: Status | None = None, progress: int | None = None, message: str | None = None, error: str | None = None) -> None:
        with self.lock:
            if status is not None:
                self.status = status
            if progress is not None:
                self.progress = progress
            if message is not None:
                self.message = message
            if error is not None:
                self.error = error


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024

JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _ensure_directories() -> None:
    for directory in (UPLOAD_DIR, PNG_DIR, RECONSTRUCTED_DIR, DECODE_UPLOAD_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _create_job(filename: str) -> Job:
    job = Job(id=uuid.uuid4().hex, filename=filename)
    with JOBS_LOCK:
        JOBS[job.id] = job
    return job


def _process_job(job: Job) -> None:
    try:
        job.update(status="processing", progress=10, message="Saving uploaded file")
        if job.original_path is None or not job.original_path.exists():
            raise FileNotFoundError("uploaded file is missing")
    except Exception as exc:  # pragma: no cover - defensive path
        job.update(status="failed", progress=0, message="Failed to persist upload", error=str(exc))
        return

    try:
        job.update(status="processing", progress=25, message="Calculating checksum")
        job.original_sha256 = sha256_file(job.original_path)

        job.update(status="processing", progress=45, message="Encoding into PNG chunks")
        job.encoded_dir = PNG_DIR / job.id
        encode_result = encode_file_to_png(job.original_path, job.encoded_dir)
        job.manifest_path = encode_result.manifest_path
        job.chunk_count = len(encode_result.chunk_paths)

        job.update(status="complete", progress=100, message=f"Encoding complete ({job.chunk_count} PNGs)")
    except Exception as exc:
        job.update(status="failed", message="Processing failed", error=str(exc))


@app.get("/")
def index() -> str:
    return render_template("OLDindex.html")

@app.post("/api/encode")
def api_encode() -> object:
    uploaded_file = request.files.get("file")
    if uploaded_file is None or uploaded_file.filename == "":
        return jsonify({"error": "Choose a file to upload."}), 400

    _ensure_directories()

    job = _create_job(uploaded_file.filename)
    safe_name = secure_filename(uploaded_file.filename) or f"upload_{job.id}"
    original_path = UPLOAD_DIR / f"{job.id}_{safe_name}"
    uploaded_file.save(original_path)
    job.original_path = original_path

    worker = threading.Thread(target=_process_job, args=(job,), daemon=True)
    worker.start()

    return jsonify({"job_id": job.id})

@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.snapshot())

@app.get("/api/jobs/<job_id>/chunks/<path:filename>")
def job_chunk(job_id: str, filename: str):
    job = _get_job(job_id)
    if job is None or job.encoded_dir is None:
        abort(404)

    # prevent traversal
    safe_name = Path(filename).name
    if safe_name != filename:
        abort(404)

    chunk_path = job.encoded_dir / safe_name
    if not chunk_path.exists() or chunk_path.suffix.lower() != ".png":
        abort(404)

    return send_file(chunk_path, mimetype="image/png")


@app.get("/api/jobs/<job_id>/download_all")
def download_all(job_id: str):
    job = _get_job(job_id)
    if job is None or job.encoded_dir is None:
        abort(404)
    if job.status != "complete":
        return jsonify({"error": "job not complete"}), 409

    encoded_dir = job.encoded_dir
    if not encoded_dir.exists():
        abort(404)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    tmp.close()

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(encoded_dir.glob("*.png")):
            # put them under a folder inside the zip for nicer UX
            z.write(file, arcname=f"{job.id}_encoded_png/{file.name}")

    @after_this_request
    def _cleanup(response):
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return response

    return send_file(
        tmp_path,
        as_attachment=True,
        download_name=f"{job.id}_encoded_png.zip",
        mimetype="application/zip",
    )


@app.post("/decode")
def decode() -> object:
    """Decode an uploaded folder or set of PNGs back into the original file."""
    files = request.files.getlist("files")
    if not files:
        return "Upload at least one encoded PNG.", 400

    _ensure_directories()

    # put all uploaded files into a per-request folder
    job_id = uuid.uuid4().hex
    target_dir = DECODE_UPLOAD_DIR / job_id
    target_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        if not f or not f.filename:
            continue
        # When the user uploads a directory, Werkzeug/Flask often includes the
        # relative path in `filename` (e.g. "folder/name.png"). We only want
        # the actual base filename so it matches the manifest's chunk list.
        safe_name = secure_filename(Path(f.filename).name)
        if not safe_name:
            continue
        f.save(target_dir / safe_name)

    if not any(target_dir.iterdir()):
        return "No valid files uploaded.", 400

    # Let png_encoder discover the manifest and reconstruct the original.
    try:
        output_path = decode_png_to_file(target_dir)
    except Exception as exc:
        return f"Decode failed: {exc}", 500

    @after_this_request
    def _cleanup(response):
        try:
            for p in target_dir.iterdir():
                p.unlink()
            target_dir.rmdir()
        except Exception:
            pass
        return response

    return send_file(
        output_path,
        as_attachment=True,
        download_name=Path(output_path).name,
    )


def _get_job(job_id: str) -> Job | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)

def _chunk_list(job_id: str, encoded_dir: Path | None) -> list[dict[str, str]]:
    if encoded_dir is None or not encoded_dir.exists():
        return []
    return [{"name": p.name, "url": f"/api/jobs/{job_id}/chunks/{p.name}"} for p in sorted(encoded_dir.glob("*.png"))]

if __name__ == "__main__":
    _ensure_directories()
    app.run(host="0.0.0.0", port=6767)