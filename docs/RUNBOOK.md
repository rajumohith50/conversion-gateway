# Conversion Gateway — Runbook

**Audience:** whoever is on call for this service.
**Assumes:** you can run `gateway` CLI commands against the production
database and queue, read the Prometheus metrics, and search the JSON logs.

Every event has one `correlation_id` (returned to the client, on the ledger
row, and on every log line in every process). When a client asks about a
conversion, ask for it first.

---

## 1. Processes and what "healthy" looks like

| Process | Health | Scrape |
| --- | --- | --- |
| `api` | `GET /readyz` → 200 (`503` = database unreachable) | `:8080/metrics` |
| `worker` | metrics port answers; `events_suppressed_total` + `events_rejected_total` + processed rate roughly equals `events_received_total` rate | `:9091/metrics` |
| `uploader` | metrics port answers; `conversions_uploaded_total` rising while there is traffic | `:9092/metrics` |
| Postgres | `pg_isready` | |
| Pub/Sub | subscription backlog near zero | |

Quick check from a shell:

```
gateway reconcile          # exit 0 and "no events stuck" = ingest→worker path is flowing
gateway dlq list           # nothing new = upload path is flowing
```

---

## 2. Alerts

### 2.1 `SuppressionRateHigh`

```promql
sum by (source) (rate(events_suppressed_total[15m]))
  / sum by (source) (rate(events_received_total[15m])) > 0.3
```

**What it means.** More than 30% of one source's events are not being uploaded
because of consent. Some suppression is normal (users opt out); a step change
is not.

**Triage.**

1. Split by reason:
   ```promql
   sum by (source, reason) (rate(events_suppressed_total[15m]))
   ```
2. Read the reason:

   | Reason ends in | Meaning | Action |
   | --- | --- | --- |
   | `_denied` | Users are opting out. | None on our side. Tell the client if it is unusually high; their consent banner may have changed. |
   | `_unspecified` | Client's CMP is not resolving a choice before the lead is created. | Client-side. Point them at Integration Guide §3.3. |
   | `_missing` | **The consent field is absent from the payload.** Almost always a tagging or mapping regression on the client side (a CRM field rename, a workflow change). | Client-side, but urgent: every event since the regression is suppressed. Confirm with one payload: `gateway dlq` will not help here — look at a recent `SUPPRESSED` event via `GET /events/{id}`; its `status_reason` and `consent_*` columns on the ledger row show exactly what arrived. |

3. Suppressed events are **not lost**. Once the client fixes their side, they
   send new events with new ids. There is no replay for suppression: the
   consent on the original row is the consent we recorded, and we do not
   override it.

### 2.2 `DlqDepthRising`

```promql
sum by (source) (dlq_depth) > 0
  and increase(conversions_dead_lettered_total[30m]) > 10
```

**What it means.** Uploads are permanently failing. `dlq_depth` is the number
of open (not yet replayed) dead-letter records.

**Triage.**

1. Group by failure class:
   ```promql
   sum by (source, conversion_action, failure_class) (increase(conversions_dead_lettered_total[30m]))
   ```
2. Look at the records:
   ```
   gateway dlq list                         # newest first
   gateway dlq list --failure-class poison
   gateway dlq show <id>                    # full payload, platform error, attempt history
   ```
3. Read the class:

   | `failure_class` | Typical reason | Cause | Fix |
   | --- | --- | --- | --- |
   | `partial` | `permanent_row:CONVERSION_ACTION_NOT_FOUND` | The client's `conversionAction` value is not configured on the platform account. | Configure the action on the platform (or fix the mapping), then replay (§3). |
   | `partial` | `permanent_row:INVALID_USER_IDENTIFIER` | A digest the platform rejected. **This is our bug** — normalisation produced something that is not a 64-char lowercase hex digest, or hashed the wrong thing. | Fix the normaliser, add the case to the fixture table, replay. |
   | `partial` | `permanent_row:EXPIRED_CLICK`, `CLICK_NOT_FOUND` | Click id too old or unknown to the platform. | Nothing to fix. Expected in small numbers; if it spikes, the client's click-id capture is broken. |
   | `permanent` | `permanent:http_401`, `http_403` | Credentials. **Every batch is failing.** | Rotate `ADS_API_TOKEN`; replay everything with `--failure-class permanent --since <when it started>`. |
   | `permanent` | `permanent:http_400` | We are sending a malformed request. Our bug. | Fix, replay. |
   | `poison` | `poison:http_503:after_6_attempts`, `poison:timeout:…` | Platform was unavailable for longer than our retry ceiling (10 min). | Confirm the platform has recovered (`upload_latency_seconds` back to normal), then replay `--failure-class poison --since <outage start>`. |
   | `poison` | `poison:row_attempt_ceiling:N` | A row-level retryable error (`TOO_RECENT_CONVERSION`) never resolved. | Usually the conversion time is wrong (before the click). Check the event; if the client's `EventTime` is bad, they must resend. |

### 2.3 `UploadLatencyHigh`

```promql
histogram_quantile(0.95, sum by (le) (rate(upload_latency_seconds_bucket{outcome="ok"}[10m]))) > 5
```

**What it means.** The platform is slow to accept batches. This precedes
timeouts and 429s.

**Triage.**

1. Check outcome mix:
   ```promql
   sum by (outcome) (rate(upload_latency_seconds_count[10m]))
   ```
   A rising `transient` share means the platform is rate-limiting or
   degraded. Retries with backoff are already happening; nothing to do
   unless it lasts.
2. Check the pipeline is not falling behind:
   ```promql
   histogram_quantile(0.95, sum by (le, source) (rate(ingest_to_upload_seconds_bucket[10m])))
   ```
   This is the number the client feels. Above 15 minutes, expect a ticket.
3. If it lasts longer than `UPLOAD_MAX_ELAPSED_SECONDS` (default 600 s), batches
   start dead-lettering as `poison`. That is by design: they are recoverable
   via replay and do not block newer traffic. Handle per §2.2 once the
   platform recovers.
4. If latency is high but `transient` is not: batch size may be too large for
   the platform's current behaviour. Reduce `UPLOAD_BATCH_SIZE` and restart the
   uploader.

### 2.4 `EventsStuckQueued`

```
gateway reconcile     # exit code 1 when anything is stuck
```

Run from a cron every 5 minutes; alert on non-zero exit.

**What it means.** Events were recorded as `QUEUED` more than
`RECONCILE_STUCK_AFTER_SECONDS` ago and the worker has not touched them.
Either the worker is down, or the API's publish to the queue failed after the
ledger commit (the two are not one transaction; see the README).

**Triage.**

1. Is the worker running and consuming? Check its metrics port and the Pub/Sub
   subscription backlog. If the backlog is large, the worker is behind, not
   broken: scale it out (the queue supports multiple consumers).
2. If the worker is healthy and the backlog is empty, the messages were never
   published. `gateway reconcile --republish` re-enqueues every stuck event
   whose match key is on the ledger row (click-id events, and any event that
   was already hashed). Events that were identifier-matched and never hashed
   are listed as `needs CRM resend`: their identifiers were only ever on the
   lost message, and the client must send them again. Give the client the
   list of `event_id`s.

### 2.5 `RejectionRateHigh`

```promql
sum by (source, reason) (rate(events_rejected_total[15m])) > 0.05 * sum by (source) (rate(events_received_total[15m]))
```

**What it means.** More than 5% of a source's events cannot be validated or
normalised.

**Triage.** The `reason` label says what:

| Reason | Where | Meaning |
| --- | --- | --- |
| `missing`, `enum`, `malformed_json` | webhook (422) | Payload shape changed on the client side. They got a 422 with the field name; usually they already know. |
| `unparseable`, `invalid`, `no_default_region` | worker | Phone data quality. Often a CRM import with a new format. |
| `not_alpha2` | worker | Client switched from the country-code field to the country-name field. |
| `no_match_key` | worker | Leads with no contact details at all. Ask the client what changed on the form. |

Rejected events are terminal and are not replayed: the data was wrong, and
the client must send corrected events with new ids. `GET /events/{id}` gives
them the exact field list.

---

## 3. Replaying after a fix

Replay is safe to run more than once: an already-replayed record is skipped,
and every conversion carries an `orderId` the platform dedupes on.

```
# 1. Confirm the fix is in place (platform config, credentials, or a deploy).

# 2. See what you are about to replay. Same filters as replay.
gateway dlq list --reason permanent_row:CONVERSION_ACTION_NOT_FOUND
gateway dlq list --failure-class poison --since 2026-09-22T01:00:00Z

# 3. Replay. Prefer the narrowest filter that covers the fix.
gateway dlq replay --id 42
gateway dlq replay --reason permanent_row:CONVERSION_ACTION_NOT_FOUND
gateway dlq replay --failure-class poison --since 2026-09-22T01:00:00Z --until 2026-09-22T02:00:00Z

# 4. Watch them go through.
gateway dlq list --include-replayed         # replayed_at set
watch 'curl -s localhost:9092/metrics | grep conversions_uploaded_total'
```

What replay does: `DEAD_LETTERED → QUEUED`, attempt counter reset, message
republished. The worker re-runs the consent gate (so a user who has since
been recorded as opted out is suppressed, not uploaded), reuses the digests
already on the row, and the uploader picks it up on its next cycle.

`gateway dlq replay` with no `--id` and no filter is refused. Replaying the
entire DLQ is almost never what you want; if it is, use `--since` with a date
before the service existed.

---

## 4. Common questions from clients

**"We sent a conversion an hour ago and it is not in the platform."**
Ask for the `event_id` or `correlation_id`. `GET /events/{id}`:
- `UPLOADED` with an `occurred_at` an hour ago: it is the platform's
  reporting delay, not us. Typically hours.
- `SUPPRESSED`: consent. Show them `status_reason`.
- `REJECTED`: show them `status_reason`; they need to resend with fixes.
- `DEAD_LETTERED`: check `gateway dlq show` for the platform's error; it is
  usually a configuration issue on their platform account.
- `QUEUED` for an hour: §2.4.

**"Our conversions doubled."**
They are retrying 202s. A 202 is success. Check `events_received_total` vs
the 200 (duplicate) responses in the API logs (`"duplicate delivery"`). No
double upload has happened: duplicates never create a second row.

**"Can you tell us which email address failed?"**
No. We do not have it. We can tell them the `event_id`, the field name and
the reason; they look up the lead in their CRM.

---

## 5. Operational changes

| Change | How | Restart needed |
| --- | --- | --- |
| Rotate a webhook secret | Set the new `WEBHOOK_SECRET_<SOURCE>`. | API |
| Rotate the platform token | Set `ADS_API_TOKEN`. | uploader |
| Add a conversion action | Platform-side configuration; nothing here. Then replay any `CONVERSION_ACTION_NOT_FOUND` records. | none |
| Change batch size / retry ceiling | `UPLOAD_*` settings. | uploader |
| Scale the worker | Run more `gateway worker` processes; Pub/Sub distributes messages. | — |
| Scale the uploader | Run more `gateway uploader` processes; the claim query uses `SKIP LOCKED`, so they do not collide. | — |
| Database migration | `alembic upgrade head` before deploying code that needs it. Migrations are additive. | api, worker, uploader after |
| Pub/Sub emulator restarted (local only) | `gateway queue-init`. | none |
