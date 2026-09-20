# First-Party Conversion Gateway

A reference implementation of a server-side gateway that receives CRM lead
events, normalises and hashes first-party identifiers, gates on consent, and
uploads offline conversions to an ad platform with durability guarantees.

The full technical design is in [docs/DESIGN.md](docs/DESIGN.md).

## Status

Built in phases. Currently implemented:

- [x] Phase 1: project skeleton, normalisation + hashing, consent gate
- [x] Phase 2: ingest API and event ledger (signature verification, schema validation, dedupe, Alembic)
- [ ] Phase 3: queue and processing worker (consent gate, normalisation)
- [ ] Phase 4: upload client and mock ads API
- [ ] Phase 5: dead-letter queue and replay CLI
- [ ] Phase 6: observability, container, runbook

## Development

```
make install   # uv sync
make run       # docker compose up (Postgres)
make migrate   # alembic upgrade head
make api       # uvicorn on :8080
make test      # pytest with coverage (floor: 90%); needs `make run`
make lint      # ruff check + format check
make typecheck # mypy --strict
make down      # docker compose down, removing volumes
```

Copy `.env.example` to `.env` before running the stack; every variable is
documented there with the phase that introduces it.

Tests run against the real Postgres from `make run`, in a separate
`gateway_test` database that is created and migrated automatically.

## Sending a webhook

```
POST /webhooks/crm/{salesforce|hubspot}
X-Webhook-Timestamp: <unix seconds>
X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>
```

| Response | Meaning |
| --- | --- |
| 202 | New event, written to the ledger as `VALIDATED` |
| 200 | Duplicate delivery; the original event id is returned, nothing written |
| 422 | Authenticated but invalid; written as `REJECTED` with structured reasons |
| 401 | Signature or timestamp failed; nothing written |

`GET /events/{event_id}` returns the lifecycle state, reason, and every
transition.
