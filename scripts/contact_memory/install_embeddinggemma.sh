#!/bin/sh
set -eu
VENV="${CONTACT_MEMORY_EMBEDDINGGEMMA_VENV:-$HOME/.cache/hermes-contact-memory/embeddinggemma-venv}"
uv venv --python 3.13 "$VENV"
uv pip install --python "$VENV/bin/python" \
  'mlx-embeddings==0.1.0' 'mlx==0.32.0' 'numpy==2.5.1' 'huggingface-hub==1.23.0'
printf '%s\n' "$VENV/bin/python"
