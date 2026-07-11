"""Persistent JSON-lines MLX Qwen3 reranker worker.

Executed only by the isolated optional contact-memory environment. The protocol
never logs query or document text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

INSTRUCTION = (
    "Retrieve the passage that entails the intended answer to the query, not "
    "merely one that shares its topic. Penalize reversed roles, opposite "
    "polarity, stale dates, different entities, and misleading lexical overlap. "
    "For questions beginning has, is, did, or does, always prefer explicit negative "
    "corrections."
)
YES_NO_INSTRUCTION = (
    "Retrieve the passage that entails the intended answer to the query, not "
    "merely one that shares its topic. Penalize reversed roles, opposite "
    "polarity, stale dates, different entities, and misleading lexical overlap. "
    "When a question begins has, is, did, or does, choose the passage with "
    "explicit negation."
)
PREFIX = (
    '<|im_start|>system\nJudge whether the Document meets the requirements '
    'based on the Query and the Instruct provided. Note that the answer can '
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-length", type=int, default=2048)
    args = parser.parse_args()

    import mlx.core as mx  # type: ignore[import-not-found]
    from mlx_lm import load  # type: ignore[import-not-found]
    from mlx_lm.models.cache import make_prompt_cache  # type: ignore[import-not-found]

    model, tokenizer = load(args.model)
    hf = getattr(tokenizer, "_tokenizer", tokenizer)
    yes_id = hf.convert_tokens_to_ids("yes")
    no_id = hf.convert_tokens_to_ids("no")
    mx.eval(model.parameters())
    sys.stdout.write(json.dumps({"ready": True}, separators=(",", ":")) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        try:
            request = json.loads(line)
            query = request.get("query")
            documents = request.get("documents")
            custom_instruction = request.get("instruction")
            instruction = custom_instruction or (
                YES_NO_INSTRUCTION
                if isinstance(query, str) and re.match(r"^\s*(?:has|is|did|does)\b", query, re.I)
                else INSTRUCTION
            )
            if not isinstance(query, str) or not isinstance(documents, list):
                raise ValueError("query and documents are required")
            if not all(isinstance(document, str) for document in documents):
                raise ValueError("documents must be strings")
            if not isinstance(instruction, str):
                raise ValueError("instruction must be a string")

            common = (
                f"{PREFIX}<Instruct>: {instruction}\n<Query>: {query}\n<Document>:"
            )
            common_ids = hf.encode(common, add_special_tokens=False)
            remaining = max(1, args.max_length - len(common_ids))
            tokenized = hf(
                [" " + document + SUFFIX for document in documents],
                add_special_tokens=False,
                truncation=True,
                max_length=remaining,
            )["input_ids"]
            rows = list(tokenized)
            lengths = [len(ids) for ids in rows]
            width = max(lengths, default=0)
            width = min(remaining, ((width + 63) // 64) * 64)
            pad_id = hf.pad_token_id if hf.pad_token_id is not None else hf.eos_token_id
            padded = [ids + [pad_id] * (width - len(ids)) for ids in rows]
            if not padded:
                scores = []
            else:
                # Prefill the instruction and query once, then broadcast its KV
                # state across all candidate documents. This removes redundant
                # top-10 prompt work while BatchKVCache preserves causal masks.
                prefix_cache = make_prompt_cache(model)
                prefix_logits = model(mx.array([common_ids]), cache=prefix_cache)
                mx.eval(prefix_logits)
                batch_cache = [layer.merge([layer] * len(rows)) for layer in prefix_cache]
                all_logits = model(mx.array(padded), cache=batch_cache)
                logits = all_logits[mx.arange(len(rows)), mx.array(lengths) - 1, :]
                # yes/no logit difference is monotonic with the model-card
                # softmax probability but retains ranking precision when both
                # probabilities round to 1.0 in low-precision inference.
                scores_array = logits[:, yes_id] - logits[:, no_id]
                mx.eval(scores_array)
                scores = [float(value) for value in scores_array.tolist()]
            response = {"ok": True, "scores": scores}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
