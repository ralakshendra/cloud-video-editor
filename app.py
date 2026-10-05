import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from google import genai

app = Flask(__name__)

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
MODEL_ID = os.environ.get("MODEL_ID", "gemini-omni-1.1-flash")
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "10"))
OUTPUT_RESOLUTION = os.environ.get("OUTPUT_RESOLUTION", "1080p")
WORK_ROOT = Path("/tmp/video_jobs")
WORK_ROOT.mkdir(parents=True, exist_ok=True)

# Render can restart a free instance, so this is intentionally an in-memory job
# tracker for the current running instance. It makes long Gemini jobs observable
# without keeping the client's HTTP request open.
jobs = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=1)


def update_job(job_id, **updates):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(updates)


def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        return dict(job) if job else None


def run_command(args):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-5000:])
    return result


def split_video(input_path: Path, out_dir: Path):
    """Split into <=10s chunks, matching Gemini Omni uploaded-video limits."""
    pattern = out_dir / "chunk_%03d.mp4"

    run_command([
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-f", "segment",
        "-segment_time", str(CHUNK_SECONDS),
        "-reset_timestamps", "1",
        str(pattern),
    ])

    chunks = sorted(out_dir.glob("chunk_*.mp4"))
    if not chunks:
        raise RuntimeError("FFmpeg did not create any chunks.")
    return chunks


def create_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add it as a secret environment variable in Render."
        )
    return genai.Client(api_key=api_key)


def file_state_name(video_file):
    state = getattr(video_file, "state", None)
    name = getattr(state, "name", None)
    return name or str(state)


def wait_for_file(client, file_name, job_id=None):
    while True:
        video_file = client.files.get(name=file_name)
        state = file_state_name(video_file)

        if job_id:
            update_job(job_id, gemini_file_state=state, gemini_file=video_file.uri)

        if state == "ACTIVE":
            return video_file
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {file_name}")

        time.sleep(5)


def edit_chunk(client, chunk_path: Path, prompt: str, output_path: Path, job_id: str, index: int, total: int):
    update_job(
        job_id,
        phase="uploading_to_gemini",
        current_chunk=index,
        total_chunks=total,
        completed_chunks=index - 1,
        gemini_file_state="UPLOADING",
    )

    print(f"Uploading {chunk_path.name} to Gemini...")
    video_file = client.files.upload(file=str(chunk_path))

    update_job(
        job_id,
        phase="waiting_for_gemini",
        gemini_file=video_file.uri,
        gemini_file_state=file_state_name(video_file),
    )

    video_file = wait_for_file(client, video_file.name, job_id)
    print(f"Gemini input ready: {video_file.uri}")

    edit_prompt = (
        f"{prompt.strip()} "
        "Keep everything else the same. Preserve the original composition, "
        "subject identity, timing, and camera movement unless the requested edit "
        "requires changing them."
    )

    update_job(job_id, phase="editing_with_gemini", gemini_file_state="ACTIVE")
    print(f"Editing {chunk_path.name} with {MODEL_ID}...")

    interaction = client.interactions.create(
        model=MODEL_ID,
        input=[
            {"type": "video", "uri": video_file.uri},
            {"type": "text", "text": edit_prompt},
        ],
        response_format={
            "type": "video",
            "delivery": "uri",
            "resolution": OUTPUT_RESOLUTION,
        },
    )

    output_video = interaction.output_video
    if not output_video or not output_video.uri:
        raise RuntimeError("Gemini returned no video URI.")

    update_job(
        job_id,
        phase="downloading_gemini_output",
        gemini_output_uri=output_video.uri,
    )

    generated_name = output_video.uri.split("/")[-1]
    wait_for_file(client, f"files/{generated_name}", job_id)

    print(f"Downloading edited {chunk_path.name}...")
    client.files.download(
        file=output_video.uri,
        destination=str(output_path),
    )

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Gemini produced an empty output for {chunk_path.name}")


def merge_videos(video_paths, output_path: Path):
    concat_file = output_path.parent / "concat.txt"

    with concat_file.open("w", encoding="utf-8") as f:
        for path in video_paths:
            f.write(f"file '{path.as_posix()}'\n")

    run_command([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ])


def process_job(job_id, job_dir, prompt):
    try:
        input_path = job_dir / "input.mp4"
        size_mb = input_path.stat().st_size / (1024 * 1024)

        if size_mb > MAX_UPLOAD_MB:
            raise RuntimeError(f"Video is larger than {MAX_UPLOAD_MB} MB.")

        update_job(job_id, status="processing", phase="splitting_video")

        chunks_dir = job_dir / "chunks"
        chunks_dir.mkdir()
        chunks = split_video(input_path, chunks_dir)
        total = len(chunks)

        update_job(
            job_id,
            phase="processing_chunks",
            total_chunks=total,
            completed_chunks=0,
            current_chunk=1,
        )

        client = create_client()
        edited_dir = job_dir / "edited"
        edited_dir.mkdir()

        edited_chunks = []
        for index, chunk in enumerate(chunks, start=1):
            edited = edited_dir / f"edited_{index:03d}.mp4"
            edit_chunk(client, chunk, prompt, edited, job_id, index, total)
            edited_chunks.append(edited)

            update_job(
                job_id,
                completed_chunks=index,
                current_chunk=index + 1 if index < total else total,
                phase="processing_chunks" if index < total else "merging_video",
                gemini_file_state="COMPLETE",
            )

        final_path = job_dir / "final_ai_edit.mp4"
        merge_videos(edited_chunks, final_path)

        update_job(
            job_id,
            status="completed",
            phase="completed",
            completed_chunks=total,
            current_chunk=total,
            download_url=f"/download/{job_id}",
        )

        print(f"Job {job_id} completed.")

    except Exception as exc:
        print(f"Job {job_id} failed: {exc}")
        update_job(
            job_id,
            status="failed",
            phase="failed",
            error=str(exc),
        )


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "cloud-video-editor",
        "model": MODEL_ID,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "gemini_configured": bool(os.environ.get("GEMINI_API_KEY")),
    })


@app.post("/edit")
def edit():
    if "video" not in request.files:
        return jsonify({"error": "Upload the video using the 'video' field."}), 400

    video = request.files["video"]
    if not video.filename:
        return jsonify({"error": "The uploaded video has no filename."}), 400

    prompt = request.form.get(
        "prompt",
        "Apply a cinematic color grade and stabilize the footage.",
    ).strip()

    job_id = uuid.uuid4().hex
    job_dir = WORK_ROOT / job_id
    job_dir.mkdir(parents=True)

    input_path = job_dir / "input.mp4"
    video.save(input_path)

    size_mb = input_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({
            "error": f"Video is larger than {MAX_UPLOAD_MB} MB."
        }), 413

    update_job(
        job_id,
        status="queued",
        phase="queued",
        total_chunks=None,
        completed_chunks=0,
        current_chunk=None,
        gemini_file_state=None,
        download_url=None,
    )

    executor.submit(process_job, job_id, job_dir, prompt)

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/status/{job_id}",
        "download_url": f"/download/{job_id}",
        "message": "Upload received. Processing has started in the background.",
    }), 202


@app.get("/status/<job_id>")
def status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({
            "error": "Job not found. Render may have restarted and cleared in-memory jobs."
        }), 404

    return jsonify({
        "job_id": job_id,
        **job,
    })


@app.get("/download/<job_id>")
def download(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found."}), 404

    if job.get("status") != "completed":
        return jsonify({
            "error": "Video is not ready yet.",
            "status": job.get("status"),
            "phase": job.get("phase"),
        }), 409

    final_path = WORK_ROOT / job_id / "final_ai_edit.mp4"
    if not final_path.exists():
        return jsonify({"error": "Final video file is no longer available."}), 404

    return send_file(
        final_path,
        as_attachment=True,
        download_name="final_ai_edit.mp4",
        mimetype="video/mp4",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
