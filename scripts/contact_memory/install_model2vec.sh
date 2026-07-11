#!/bin/sh
set -eu
VENV="${CONTACT_MEMORY_MODEL2VEC_VENV:-$HOME/.cache/hermes-contact-memory/model2vec-venv}"
uv venv --python 3.13 "$VENV"
uv pip install --python "$VENV/bin/python" 'model2vec==0.8.2' 'numpy==2.4.3'
printf '%s\n' "$VENV/bin/python"
