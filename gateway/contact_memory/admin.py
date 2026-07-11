"""Operational review/export/delete and activation checks for contact memory.

Run as ``python -m gateway.contact_memory.admin``. Destructive deletion requires
an explicit confirmation token. The activation check emits counts and booleans
only; private fact text is never printed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home

from .schema import Audience, MentionPolicy, RetrievalPrincipal
from .store import ContactMemoryStore


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raw = getattr(value, "value", None)
    if raw is not None:
        return raw
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def resolve_contact_memory_root(*, root: str | None = None, profile: str | None = None) -> Path:
    """Resolve storage at the selected profile home, not the process profile."""
    if root and profile:
        raise ValueError("--root and --profile are mutually exclusive")
    if root:
        return Path(root).expanduser()
    if profile:
        from hermes_cli.profiles import get_profile_dir
        return get_profile_dir(profile) / "contact-memory"
    return get_hermes_home() / "contact-memory"


def _load_contact_memory_config(profile_home: Path) -> dict[str, object]:
    import yaml

    config_path = profile_home / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    agent = raw.get("agent") if isinstance(raw, dict) else None
    config = agent.get("contact_memory") if isinstance(agent, dict) else None
    if not isinstance(config, dict):
        raise RuntimeError(f"contact memory is not configured in {config_path}")
    return config


def activation_self_check(
    root: Path,
    contact_id: str,
    config: dict[str, object],
    principal: RetrievalPrincipal,
) -> dict[str, object]:
    """Exercise one configured namespace and return text-free diagnostics."""
    from .broker import ContactMemoryBroker, RetrievalScope
    from .embeddings import backend_from_config
    from .rerankers import reranker_from_config

    if not config.get("enabled") or not config.get("lane_a"):
        raise RuntimeError("contact-memory Lane A is not enabled")
    embedding = backend_from_config(config)
    reranker = reranker_from_config(config)
    if embedding is None:
        raise RuntimeError("configured embedding backend is required")
    if reranker is None:
        raise RuntimeError("configured reranker is required")
    store = ContactMemoryStore(root, contact_id)
    facts = store.active_facts(principal)
    if not facts:
        raise RuntimeError("configured store has no active authorized facts")
    try:
        probe = embedding.encode(["Synthetic activation readiness probe."])
        if len(probe) != 1 or not probe[0]:
            raise RuntimeError("embedding worker returned no probe vector")
        dimensions = len(probe[0])
        coverage = store.embedding_coverage(principal, embedding.model_id)
        if not coverage["complete"] or coverage["matching_dimensions"] != [dimensions]:
            raise RuntimeError("active authorized facts do not have matching configured vectors")

        scores = reranker.score(
            "Which synthetic note mentions readiness?",
            ["A synthetic readiness note.", "A synthetic unrelated note."],
        )
        if len(scores) != 2:
            raise RuntimeError("reranker smoke returned the wrong score count")

        # This authorized row is used only in-process. Neither the query nor the
        # rendered recall is included in output or logs.
        query = f"{facts[0].predicate} {facts[0].object_text}"
        bundle = ContactMemoryBroker(
            root, embedding_backend=embedding, reranker=reranker,
        ).search(
            RetrievalScope(principal, contact_id, "activation-self-check"),
            query,
            limit=1,
        )
        if bundle.empty:
            raise RuntimeError("synthetic retrieval returned no authorized fact")
        return {
            "ok": True,
            "principal": principal.value,
            "active_authorized_facts": coverage["active_authorized_facts"],
            "matching_vectors": coverage["matching_vectors"],
            "vector_dimensions": dimensions,
            "embedding_model_matches": True,
            "embedding_worker_smoke": True,
            "reranker_worker_smoke": True,
            "synthetic_retrieval": True,
        }
    finally:
        close = getattr(embedding, "close", None)
        if callable(close):
            close()
        close = getattr(reranker, "close", None)
        if callable(close):
            close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and manage one contact-memory namespace")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--root", help="contact-memory root (defaults to profile HERMES_HOME)")
    parser.add_argument("--profile", help="named Hermes profile whose configured store should be used")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("review", help="list pending proposals")

    accept = commands.add_parser("accept", help="accept one pending proposal")
    accept.add_argument("proposal_id")
    accept.add_argument("--audience", choices=[v.value for v in Audience], default="owner_only")
    accept.add_argument("--mention-policy", choices=[v.value for v in MentionPolicy], default="background")

    reject = commands.add_parser("reject", help="reject one pending proposal")
    reject.add_argument("proposal_id")

    export = commands.add_parser("export", help="export active owner-visible facts")
    export.add_argument("--output", required=True)

    delete = commands.add_parser("delete", help="secure-delete all rows in the namespace")
    delete.add_argument("--confirm", required=True, help="must exactly equal the contact ID")

    index = commands.add_parser("index", help="embed all approved active facts")
    index.add_argument("--backend", default="embeddinggemma", choices=["embeddinggemma"])
    index.add_argument("--model", default="mlx-community/embeddinggemma-300m-4bit")
    index.add_argument("--python-path")

    check = commands.add_parser("self-check", help="verify activation without printing private fact text")
    check.add_argument("--principal", choices=[v.value for v in RetrievalPrincipal], required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        root = resolve_contact_memory_root(root=args.root, profile=args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    store = ContactMemoryStore(root, args.contact_id)

    if args.command == "review":
        result: object = store.list_pending()
    elif args.command == "accept":
        result = store.decide_pending(
            args.proposal_id,
            accept=True,
            audience=Audience(args.audience),
            mention_policy=MentionPolicy(args.mention_policy),
        )
    elif args.command == "reject":
        result = {"rejected": bool(store.decide_pending(args.proposal_id, accept=False) is None)}
    elif args.command == "export":
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(store.export_owner(), ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        try:
            output.chmod(0o600)
        except OSError:
            pass
        result = {"exported": str(output)}
    elif args.command == "index":
        from .broker import ContactMemoryBroker
        from .embeddings import EmbeddingGemmaBackend
        backend = EmbeddingGemmaBackend(args.model, python_path=args.python_path)
        try:
            count = ContactMemoryBroker(root, embedding_backend=backend).index_approved_facts(args.contact_id)
        finally:
            backend.close()
        result = {"indexed": count, "model": args.model}
    elif args.command == "self-check":
        profile_home = root.parent if root.name == "contact-memory" else get_hermes_home()
        config = _load_contact_memory_config(profile_home)
        result = activation_self_check(
            root, args.contact_id, config, RetrievalPrincipal(args.principal)
        )
    else:
        if args.confirm != args.contact_id:
            parser.error("--confirm must exactly equal --contact-id")
        store.secure_delete_all()
        result = {"deleted": True}

    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
