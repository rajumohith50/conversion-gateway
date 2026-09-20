# First-Party Conversion Gateway

A reference implementation of a server-side gateway that receives CRM lead
events, normalises and hashes first-party identifiers, gates on consent, and
uploads offline conversions to an ad platform with durability guarantees.

The full technical design is in [docs/DESIGN.md](docs/DESIGN.md).

## Status

Built in phases. Currently implemented:

- [x] Phase 1: project scaffold, normalisation + hashing, consent gate
- [ ] Phase 2: event ledger (Postgres, SQLAlchemy, Alembic)
- [ ] Phase 3: ingest API (signature verification, schema validation, dedupe)
- [ ] Phase 4: queue, processor, upload client, mock ads API
- [ ] Phase 5: dead-letter queue and replay CLI
- [ ] Phase 6: observability, container, runbook

## Development

```
make install   # uv sync
make test      # pytest
make lint      # ruff
make typecheck # mypy --strict
```
