# Video Tool — Local Transcriber + Downloader

Paste any video link (YouTube, TikTok, Instagram, X, etc.), transcribe it, download it, or both. Runs 100% locally.

## Setup

### 1. Prerequisites
- **Python 3.8+**
- **ffmpeg** — `brew install ffmpeg`

### 2. Install dependencies
```bash
cd video-tool
pip install -r requirements.txt
```

### 3. Run
```bash
python app.py
```

Open **http://localhost:5000**

## How it works

1. Paste a video URL
2. Check **Transcribe**, **Download**, or both
3. Hit **Go**
4. Your selections are remembered — spam links without re-checking boxes

## Supported platforms
Anything yt-dlp supports (1000+ sites): YouTube, TikTok, Instagram, X/Twitter, Facebook, Vimeo, Reddit, etc.

## Whisper models

| Model  | Speed    | Accuracy | Size     |
|--------|----------|----------|----------|
| tiny   | Fastest  | Basic    | ~75 MB   |
| base   | Good     | Good     | ~140 MB  |
| small  | Slower   | Better   | ~460 MB  |
| medium | Slowest  | Best     | ~1.5 GB  |

Model selection is also remembered between sessions.

*Everything runs locally. No data sent anywhere.*
