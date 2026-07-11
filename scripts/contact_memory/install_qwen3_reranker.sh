#!/bin/sh
set -eu
VENV="${CONTACT_MEMORY_MLX_VENV:-$HOME/.cache/hermes-contact-memory/embeddinggemma-venv}"
MODEL="${CONTACT_MEMORY_RERANKER_MODEL:-mlx-community/Qwen3-Reranker-0.6B-4bit}"
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --python 3.13 "$VENV"
fi
uv pip install --python "$VENV/bin/python" 'mlx-lm==0.31.3' 'huggingface-hub==1.23.0'
"$VENV/bin/python" -c 'from huggingface_hub import snapshot_download; import sys; snapshot_download(sys.argv[1])' "$MODEL"
printf '%s\n' "$VENV/bin/python"
