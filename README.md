# Cloud Video Editor

Cloud video editing service using Render, Docker, FFmpeg and Gemini Omni Flash.

## What it does

1. Receives a raw video from Windows with curl.
2. Splits it into <=10-second chunks.
3. Uploads each chunk to Gemini.
4. Applies your editing prompt with Gemini Omni Flash.
5. Downloads each generated result.
6. Merges the edited chunks with FFmpeg.
7. Returns `final_ai_edit.mp4`.

Gemini Omni's uploaded-video editing limit is 10 seconds, so this uses 10-second chunks rather than the old 30-second split.

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

## Process a video

```cmd
curl -X POST -F "video=@my_raw_video.mp4" -F "prompt=Apply a cinematic color grade and stabilize the footage. Keep everything else the same." https://YOUR-APP.onrender.com/edit --output final_ai_edit.mp4
```

## Important

Each 10-second chunk is independently edited. That means cuts can occur at chunk boundaries and Gemini may make slightly different decisions between chunks.

The Render filesystem is temporary. The service is designed to return the final file immediately rather than use Render as permanent storage.

For longer jobs, an asynchronous queue/job endpoint is the next production improvement.
