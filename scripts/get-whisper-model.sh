#!/usr/bin/env bash
# Downloads the Whisper large-v3-turbo model (~1.6 GB) — good Hindi/English/Hinglish accuracy, fast on M-series.
set -euo pipefail
DIR="${1:-$HOME/.devagent/models}"
MODEL="ggml-large-v3-turbo.bin"
mkdir -p "$DIR"
if [[ -f "$DIR/$MODEL" ]]; then echo "Whisper model already present: $DIR/$MODEL"; exit 0; fi
echo "Downloading $MODEL to $DIR …"
curl -L --fail -o "$DIR/$MODEL.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"
mv "$DIR/$MODEL.part" "$DIR/$MODEL"
echo "Done."
