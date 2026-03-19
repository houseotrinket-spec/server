#!/usr/bin/env bash
set -e

pip install -r requirements.txt

# Download static ffmpeg binary directly — no apt-get needed
mkdir -p /opt/ffmpeg
curl -L https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
  | tar -xJ --strip-components=2 -C /opt/ffmpeg
export PATH="/opt/ffmpeg:$PATH"
echo "ffmpeg installed: $(ffmpeg -version | head -1)"
