"""Flask interface for uploading files and monitoring processing status."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from png_encoder import (
    decode_png_to_file,
    encode_file_to_png,
    sha256_file,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
PNG_DIR = BASE_DIR / "encoded_png"
RECONSTRUCTED_DIR = BASE_DIR / "reconstructed"


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
    reconstructed_sha256: str | None = None
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
                "reconstructed_sha256": self.reconstructed_sha256,
                "chunk_count": self.chunk_count,
                "error": self.error,
                "encoded_dir": str(self.encoded_dir) if self.encoded_dir else None,
                "manifest_path": str(self.manifest_path) if self.manifest_path else None,
                "reconstructed_path": str(self.reconstructed_path) if self.reconstructed_path else None,
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
    for directory in (UPLOAD_DIR, PNG_DIR, RECONSTRUCTED_DIR):
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
        job.encoded_dir = PNG_DIR
        encode_result = encode_file_to_png(job.original_path, job.encoded_dir)
        job.manifest_path = encode_result.manifest_path
        job.chunk_count = len(encode_result.chunk_paths)

        job.update(status="processing", progress=70, message="Reconstructing from manifest")
        job.reconstructed_path = decode_png_to_file(job.manifest_path, RECONSTRUCTED_DIR / job.filename)

        job.update(status="processing", progress=90, message="Verifying checksum")
        job.reconstructed_sha256 = sha256_file(job.reconstructed_path)
        if job.original_sha256 != job.reconstructed_sha256:
            raise ValueError("checksum mismatch after reconstruction")

        job.update(status="complete", progress=100, message=f"Processing complete ({job.chunk_count} chunks)")
    except Exception as exc:
        job.update(status="failed", message="Processing failed", error=str(exc))


@app.get("/")
def index() -> str:
    return render_template("index.html", jobs=list(_latest_jobs()))


@app.post("/upload")
def upload() -> object:
    uploaded_file = request.files.get("file")
    if uploaded_file is None or uploaded_file.filename == "":
        return render_template("index.html", jobs=list(_latest_jobs()), error="Choose a file to upload."), 400

    _ensure_directories()
    job = _create_job(uploaded_file.filename)
    safe_name = secure_filename(uploaded_file.filename) or f"upload_{job.id}"
    original_path = UPLOAD_DIR / f"{job.id}_{safe_name}"
    uploaded_file.save(original_path)
    job.original_path = original_path

    worker = threading.Thread(target=_process_job, args=(job,), daemon=True)
    worker.start()

    return redirect(url_for("job_page", job_id=job.id))


@app.get("/jobs/<job_id>")
def job_page(job_id: str) -> str:
    job = _get_job(job_id)
    if job is None:
        return render_template("index.html", jobs=list(_latest_jobs()), error="Unknown job id."), 404

    return render_template("index.html", job=job.snapshot(), jobs=list(_latest_jobs()))


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.snapshot())


def _get_job(job_id: str) -> Job | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def _latest_jobs(limit: int = 8):
    with JOBS_LOCK:
        jobs = list(JOBS.values())[-limit:]
    return [job.snapshot() for job in reversed(jobs)]


if __name__ == "__main__":
    _ensure_directories()
    app.run(host="127.0.0.1", port=5000, debug=True)