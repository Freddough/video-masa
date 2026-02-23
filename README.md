# Video Masa — Local Video Transcriber + Downloader

Paste any video link (YouTube, TikTok, Instagram, X, etc.), transcribe it, download it, or both. Runs 100% locally — no data sent anywhere.

## Features

- **Transcribe** videos with OpenAI Whisper (tiny, base, small, medium models)
- **Download** videos as MP4 or extract audio as MP3
- **Quality selection** — choose video resolution (1080p, 720p, etc.) and audio bitrate independently
- **Quality history** — re-downloading at a different quality creates a new job, preserving your original
- **File upload** — drag-and-drop or browse local video/audio files for transcription
- **Browser cookies** — pass login cookies from Chrome, Firefox, Safari, etc. for sites that require authentication
- **Auto-paste** — toggle automatic URL submission when pasting from clipboard
- **Desktop app** — distributable macOS (.dmg) and Windows (.zip) packages available

## Installation

### Option A: Run from source

#### 1. Prerequisites
- **Python 3.8+**
- **ffmpeg** — `brew install ffmpeg` (macOS) or [download](https://ffmpeg.org/download.html)

#### 2. Install dependencies
```bash
cd video-masa
pip install -r requirements.txt
```

#### 3. Run
```bash
python app.py
```

Open **http://localhost:8080** in your browser.

### Option B: Desktop app

Pre-built packages are available for macOS and Windows. Download the latest release from the [Releases](../../releases) page.

**macOS**: Download the `.dmg`, drag Video Masa to Applications, and double-click to run.

**Windows**: Download the `.zip`, extract, and run `launcher.bat`.

Both packages handle Python environment setup and dependency installation automatically on first launch.

#### Building from source

```bash
# macOS — creates dist/VideoMasa-2.0.dmg
bash packaging/macos/build_dmg.sh

# Windows — creates dist/VideoMasa-2.0-Windows.zip
bash packaging/windows/build_zip.sh
```

## How it works

1. Paste a video URL (or drag-and-drop a file)
2. Toggle **Transcribe**, **Download**, or both
3. Hit **+ Add**
4. Jobs appear in the queue with real-time progress
5. Completed jobs show transcripts, download buttons, and quality options
6. Re-download at a different quality — each download is preserved as a separate job

## Supported platforms

Anything yt-dlp supports (1000+ sites): YouTube, TikTok, Instagram, X/Twitter, Facebook, Vimeo, Reddit, etc.

## Whisper models

| Model  | Speed    | Accuracy | Size     |
|--------|----------|----------|----------|
| tiny   | Fastest  | Basic    | ~75 MB   |
| base   | Good     | Good     | ~140 MB  |
| small  | Slower   | Better   | ~460 MB  |
| medium | Slowest  | Best     | ~1.5 GB  |

Model and preference selections are remembered between sessions.

## What's new in v2.0

- **Quality history** — re-downloading at a different quality spawns a new job instead of replacing the original
- **Quality badges** — each job card shows what resolution or bitrate it was downloaded at
- **Split quality controls** — separate video resolution and audio bitrate dropdowns above MP4/MP3 buttons
- **Audio bitrate selection** — choose specific audio quality when downloading MP3
- **Browser cookies support** — authenticate with sites like Twitter/X by passing browser cookies to yt-dlp
- **Redesigned controls** — two-tier layout with cleaner organization of toggles and settings
- **Auto-paste toggle** — quickly enable/disable automatic URL submission

*Everything runs locally. No data sent anywhere.*
