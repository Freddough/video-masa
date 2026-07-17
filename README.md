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
- **Python 3.10+**
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

Open the authenticated local URL printed in the terminal.

### Option B: Desktop app

Pre-built packages are available for macOS and Windows. Download the latest release from the [Releases](../../releases) page.

**macOS**: Download the `.dmg`, drag Video Masa to Applications, and double-click to run.

**Windows**: Download the `.zip`, extract, and run `launcher.bat`.

Both packages handle Python environment setup and dependency installation automatically on first launch. The desktop runtime includes a pinned, architecture-specific ffmpeg binary, so Homebrew is not required.

#### Building from source

```bash
# macOS — creates dist/VideoMasa-3.0.3.dmg
bash packaging/macos/build_dmg.sh

# Windows — creates the versioned Windows zip
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

## What's new in v3.0.3

- **Native Apple Silicon launch** — detects Rosetta-translated startup and re-executes the launcher and setup under ARM64 so Python matches architecture-specific dependencies
- **Architecture diagnostics** — health and launch reports now identify the active Python architecture

## What's new in v3.0.2

- **Protected-bundle setup fix** — verifies Python source without writing `__pycache__` inside the signed app under `/Applications`

## What's new in v3.0.1

- **Self-healing macOS runtime** — detects broken Python environments and opens repair setup automatically
- **Atomic setup** — verifies a versioned replacement runtime before switching to it and preserves the previous environment
- **Reliable launch diagnostics** — waits for server readiness and offers repair, log, and diagnostic actions on failure
- **Safer local API** — per-launch authentication, loopback origin checks, upload/job limits, and private cookie storage
- **Security fixes** — blocks cookie path traversal and renders job data through DOM properties instead of HTML strings

## What's new in v2.3

- **DMG install experience** — opening the DMG now shows a branded dark background with an arrow and "Drag Video Masa into Applications" text, plus a custom Applications folder icon
- **Persistent cookies** — cookie files now survive app upgrades by storing them in `~/.videomasa/cookies/` instead of inside the app bundle

## What's new in v2.2

- **5-minute inactivity timeout** — app now waits 5 minutes (up from 90 seconds) before auto-shutting down
- **Shutdown overlay** — when the app closes due to inactivity, the browser shows a friendly message explaining what happened and how to restart

## What's new in v2.1

- **Persistent cookie files** — upload and name cookie files that survive restarts; manage saved cookies from a dropdown
- **Cookie info modal** — in-app guide explaining browser cookies, custom cookies, and how to export
- **MP3 download fix** — resolved "File not available" error when downloading MP3
- **Transcription fix** — resolved "output not found" error with robust Whisper output detection
- **UI polish** — brighter label contrast, lime-green brand accents on empty state and info button

## What's new in v2.0

- **Quality history** — re-downloading at a different quality spawns a new job instead of replacing the original
- **Quality badges** — each job card shows what resolution or bitrate it was downloaded at
- **Split quality controls** — separate video resolution and audio bitrate dropdowns above MP4/MP3 buttons
- **Audio bitrate selection** — choose specific audio quality when downloading MP3
- **Browser cookies support** — authenticate with sites like Twitter/X by passing browser cookies to yt-dlp
- **Redesigned controls** — two-tier layout with cleaner organization of toggles and settings
- **Auto-paste toggle** — quickly enable/disable automatic URL submission

*Everything runs locally. No data sent anywhere.*
