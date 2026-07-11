"""JSON-lines MLX worker; executed only by the isolated optional environment."""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    from mlx_embeddings import load  # type: ignore[import-not-found]

    model, tokenizer = load(args.model)
    sys.stdout.write('{"ready":true}\n')
    sys.stdout.flush()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            texts = request.get("texts")
            kind = request.get("kind", "query")
            if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
                raise ValueError("texts must be a list of strings")
            prefix = "title: none | text: " if kind == "document" else "task: search result | query: "
            encoded = tokenizer(
                [prefix + text for text in texts],
                padding=True,
                truncation=True,
                return_tensors="mlx",
            )
            output = model(encoded["input_ids"], encoded["attention_mask"])
            response = {"ok": True, "vectors": output.text_embeds.tolist()}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
