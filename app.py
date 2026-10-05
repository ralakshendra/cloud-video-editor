import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid
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
    """Split into <=10s chunks, which matches Gemini Omni uploaded-video editing limits."""
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


def wait_for_file(client, file_name):
    while True:
        video_file = client.files.get(name=file_name)
        state = getattr(video_file.state, "name", str(video_file.state))

        if state == "ACTIVE":
            return video_file
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {file_name}")

        time.sleep(5)


def edit_chunk(client, chunk_path: Path, prompt: str, output_path: Path):
    print(f"Uploading {chunk_path.name} to Gemini...")
    video_file = client.files.upload(file=str(chunk_path))

    video_file = wait_for_file(client, video_file.name)
    print(f"Gemini input ready: {video_file.uri}")

    edit_prompt = (
        f"{prompt.strip()} "
        "Keep everything else the same. Preserve the original composition, "
        "subject identity, timing, and camera movement unless the requested edit "
        "requires changing them."
    )

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

    # Google-hosted generated video files become ACTIVE before download.
    generated_name = output_video.uri.split("/")[-1]
    wait_for_file(client, f"files/{generated_name}")

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

    # Re-encode the final file so differences between generated chunks do not
    # prevent concatenation.
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

    try:
        size_mb = input_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_UPLOAD_MB:
            return jsonify({
                "error": f"Video is larger than {MAX_UPLOAD_MB} MB."
            }), 413

        print(f"Starting job {job_id}")
        chunks_dir = job_dir / "chunks"
        chunks_dir.mkdir()

        chunks = split_video(input_path, chunks_dir)
        print(f"Created {len(chunks)} chunks.")

        client = create_client()
        edited_dir = job_dir / "edited"
        edited_dir.mkdir()

        edited_chunks = []
        for index, chunk in enumerate(chunks, start=1):
            edited = edited_dir / f"edited_{index:03d}.mp4"
            edit_chunk(client, chunk, prompt, edited)
            edited_chunks.append(edited)

        final_path = job_dir / "final_ai_edit.mp4"
        merge_videos(edited_chunks, final_path)

        return send_file(
            final_path,
            as_attachment=True,
            download_name="final_ai_edit.mp4",
            mimetype="video/mp4",
        )

    except Exception as exc:
        print(f"Job {job_id} failed: {exc}")
        return jsonify({
            "error": "Video processing failed.",
            "details": str(exc),
        }), 500
    finally:
        # Keep the downloaded response alive while Flask serves it, then clean up.
        # Render's filesystem is ephemeral anyway.
        pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
