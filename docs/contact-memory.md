# Contact-aware retrieval

Contact memory is local, contact-scoped SQLite state used only by trusted gateway routes. It is disabled by default.

```yaml
agent:
  contact_memory:
    enabled: false
    lane_a: false
```

These switches are reserved and **do not enable injection yet**. Lane A is hard-blocked even if both are set to true because Hermes currently cache-marks the complete system block; changing it with per-turn recall would invalidate the provider prompt cache. Promotion requires a core provider-request suffix that is demonstrably after the final cache breakpoint. Recall is never moved into transcript/history as a workaround.

The storage, broker, importer, and review plumbing are usable for offline evaluation. There is no live gateway scope handoff. No Lane B tool, owner/Poke path, or owner shadow mode is implemented or claimed.

Data lives under `$HERMES_HOME/contact-memory/contacts/` using SHA-256-derived opaque filenames. Guest visibility requires an active, stated, high-trust/high-confidence `guest_ok` or `public` fact. Restricted, sensitive, inferred, pending, quarantined, withdrawn, and superseded facts are excluded before scoring and checked again before rendering.

## Import and review

Dry-run first (default):

```bash
python -m gateway.contact_memory.import_contacts dossier.jsonl --contact-id contact-id --manifest /tmp/review.json
```

Apply only after reviewing the manifest:

```bash
python -m gateway.contact_memory.import_contacts dossier.jsonl --contact-id contact-id --apply
```

The manifest contains counts, source IDs, and validation errors, not fact text. Existing sensitive, third-party, and ambiguous rows become owner-only/quarantined. Guest visibility additionally requires explicit `guest_reviewed: true` and `audience: guest_ok`.

Imports are one transaction and idempotent by source ID. Retrying a dossier skips existing source IDs; a quarantined proposal cannot supersede an already reviewed active fact.

Review pending extractor proposals and accept or reject them explicitly:

```bash
python -m gateway.contact_memory.admin --contact-id contact-id review
python -m gateway.contact_memory.admin --contact-id contact-id accept PROPOSAL_ID --audience owner_only
python -m gateway.contact_memory.admin --contact-id contact-id reject PROPOSAL_ID
```

Export owner-visible active facts or securely clear the namespace:

```bash
python -m gateway.contact_memory.admin --contact-id contact-id export --output /secure/path/export.json
python -m gateway.contact_memory.admin --contact-id contact-id delete --confirm contact-id
```

Deletion enables SQLite secure-delete, checkpoints/truncates WAL, and vacuums the database. Filesystem snapshots and backups are outside SQLite's deletion boundary and must be handled separately.

## Optional Model2Vec benchmark

Model2Vec and NumPy are intentionally not core dependencies. Install into an isolated local environment and benchmark only synthetic data:

```bash
scripts/contact_memory/install_model2vec.sh
~/.cache/hermes-contact-memory/model2vec-venv/bin/python scripts/contact_memory/benchmark_model2vec.py --offline-check
```

Pinned tooling: Model2Vec 0.8.2 (MIT), NumPy 2.4.3 (BSD-3-Clause). The candidate model is `minishlab/potion-retrieval-32M`; record the resolved Hugging Face revision from the local cache before promotion. Do not enable retrieval solely from this tiny synthetic smoke benchmark.

### Mac Studio result (2026-07-11)

- Model revision: `6fc8051fab2a1e0ee76689cf08c853792ac285e7`
- Python 3.13.12 / Darwin arm64; native install succeeded
- Cold load/download: 4259.51 ms; warm encode p50: 0.23 ms; max: 6.68 ms
- Process max RSS: 615.69 MiB; offline reload: passed
- Synthetic direction/negation/date/reference top-1: 3/5

This candidate fails the initial quality smoke gate despite excellent warm latency. It remains tooling-only and Lane A stays hard-blocked; no production embedding model is selected.
