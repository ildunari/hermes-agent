"""Operational review/export/delete CLI for profile-local contact memory.

Run as ``python -m gateway.contact_memory.admin``.  Destructive deletion requires
an explicit confirmation token so this module is safe to expose as documented
operator plumbing without adding a model tool.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Iterable

from hermes_constants import get_hermes_home

from .schema import Audience, MentionPolicy
from .store import ContactMemoryStore


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raw = getattr(value, "value", None)
    if raw is not None:
        return raw
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and manage one contact-memory namespace")
    parser.add_argument("--contact-id", required=True)
    parser.add_argument("--root", help="contact-memory root (defaults to profile HERMES_HOME)")
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

    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).expanduser() if args.root else get_hermes_home() / "contact-memory"
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
    else:
        if args.confirm != args.contact_id:
            parser.error("--confirm must exactly equal --contact-id")
        store.secure_delete_all()
        result = {"deleted": True}

    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
