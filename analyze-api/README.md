# Analyze API

An independent FastAPI worker service that scans inspiration videos and analyzes them with Xiaomi MiMo video understanding.

## Processing Flow

1. The scheduler scans for pending jobs every `APP_ANALYSIS_SCAN_INTERVAL_SECONDS` (default 10 seconds).
2. It atomically claims the oldest pending inspiration job.
3. It requests the protected video from fetch-api using the shared internal token.
4. It sends the video to `mimo-v2.5` as Base64 with `fps=2` and default media resolution.
5. It stores the transcript, background music, video description, and content understanding in SQLite.
6. Jobs are processed sequentially. Failed jobs retry up to three times before moving to `failed`.

The service follows the official [Xiaomi MiMo video understanding guide](https://mimo.mi.com/docs/zh-CN/quick-start/usage-guide/multimodal-understanding/video-understanding).

## Setup

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure the root `.env` file. Set the same private internal token through:

```text
APP_INTERNAL_API_TOKEN
```

Restart fetch-api once before starting this service so the shared database schema is migrated.

## Start

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8090
```

The project-level `start.py`, `stop.py`, and `restart.py` scripts also manage this service.

Health check:

```text
GET http://127.0.0.1:8090/api/health
```

For local diagnostics, a protected manual scan is available:

```bash
curl -X POST http://127.0.0.1:8090/api/jobs/run-once \
  -H "X-Internal-Token: YOUR_INTERNAL_TOKEN"
```

## Limits

MiMo Base64 video input must remain below 50 MB. This service limits the downloaded source video to approximately 37 MB before Base64 encoding. Supported source formats are MP4, MOV, AVI, and WMV, subject to model compatibility.

`APP_MIMO_VIDEO_INPUT_MODE` supports:

- `auto`: use Base64 for small videos, then fall back to a refreshed platform CDN URL when the source exceeds 37 MB.
- `base64`: always use Base64 and reject oversized videos.
- `url`: always send a video URL to MiMo.

For the most reliable URL mode, expose fetch-api through a public HTTPS URL and set `APP_PUBLIC_FETCH_API_URL`. The service then creates a short-lived HMAC-signed proxy URL. Without a public URL, URL mode uses the platform's refreshed signed CDN URL, which may still be rejected by platform anti-hotlink rules.
