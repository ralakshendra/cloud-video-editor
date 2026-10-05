# Cloud Video Editor

Cloud video editing service using Render, Docker, FFmpeg and Gemini Omni Flash.

## What it does

1. Receives a raw video from Windows with curl.
2. Splits it into <=10-second chunks.
3. Uploads each chunk to Gemini.
4. Applies your editing prompt with Gemini Omni Flash.
5. Downloads each generated result.
6. Merges the edited chunks with FFmpeg.
7. Lets you monitor the job and download the final video when ready.

Gemini Omni's uploaded-video editing limit is 10 seconds, so this uses 10-second chunks.

## Render environment variables

Required:

- `GEMINI_API_KEY`

Optional:

- `MODEL_ID` = `gemini-omni-1.1-flash`
- `OUTPUT_RESOLUTION` = `1080p`
- `CHUNK_SECONDS` = `10`
- `MAX_UPLOAD_MB` = `500`

## Test health

```cmd
curl https://YOUR-APP.onrender.com/
```

## Start a video job

Use this command from the folder containing your video:

```cmd
curl -X POST -F "video=@VID_20260927_093207_392_bsl.mp4" -F "prompt=Apply a cinematic color grade and stabilize the footage. Keep everything else the same." https://YOUR-APP.onrender.com/edit
```

The response is immediate and looks like:

```json
{
  "job_id": "abc123...",
  "status": "queued",
  "status_url": "/status/abc123...",
  "download_url": "/download/abc123..."
}
```

## Check job status

Replace `JOB_ID` with the returned ID:

```cmd
curl https://YOUR-APP.onrender.com/status/JOB_ID
```

The response includes:

- `status`: queued, processing, completed, or failed
- `phase`: current processing stage
- `current_chunk` and `total_chunks`
- `completed_chunks`
- `gemini_file_state`: the current Gemini Files API state
- `gemini_file`: the Gemini file URI when available
- `error`: details if the job fails

## Download the finished video

After status reports `"status": "completed"`:

```cmd
curl https://YOUR-APP.onrender.com/download/JOB_ID --output final_ai_edit.mp4
```

## Important

Each 10-second chunk is independently edited. Gemini may make slightly different decisions between chunks.

The Render filesystem is temporary, and the job tracker is in memory. If Render restarts while a job is running, the job and temporary files can be lost.
