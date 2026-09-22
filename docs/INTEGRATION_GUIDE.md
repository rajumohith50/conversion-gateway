# Conversion Gateway — Integration Guide

**Audience:** the engineer on your side who owns the CRM webhook.
**What you get at the end:** closed-won (or any other downstream outcome) events
from your CRM arrive at the ad platform as offline conversions, matched back to
the original ad click, within minutes.

This guide covers what to send, how to sign it, what we do with it, and what
you will see when something is rejected. If anything here is unclear, the
`GET /events/{event_id}` endpoint (section 7) will usually answer the question
faster than a support ticket.

---

## 1. How it works

1. Your CRM sends us a webhook when a lead reaches the outcome you want to
   measure (typically *closed-won*, but any stage works).
2. We verify the signature, validate the payload, and acknowledge within
   ~50 ms. **A 202 means "durably recorded", not "uploaded".**
3. Asynchronously, we check consent, normalise and hash the identifiers, and
   upload the conversion to the platform.
4. You can query the state of any event by its id at any time.

Two things never happen: we never store raw personal data (email, phone,
name, street address) beyond the lifetime of the webhook request, and we never
upload anything for a user who has not granted consent.

---

## 2. Endpoint

```
POST https://<gateway-host>/webhooks/crm/salesforce
POST https://<gateway-host>/webhooks/crm/hubspot
Content-Type: application/json
X-Webhook-Timestamp: <unix seconds>
X-Webhook-Signature: sha256=<hex>
```

Which path you use depends only on which payload shape you send (section 3).
If your CRM is neither, send the shape you can produce most faithfully and
tell us; adding a source is a small change on our side.

---

## 3. Payload contract

### 3.1 Salesforce-shaped

```json
{
  "EventId": "e-1001",
  "EventTime": "2026-09-22T09:30:00Z",
  "Conversion_Action__c": "closed_won",
  "Amount": 4800.00,
  "CurrencyIsoCode": "USD",
  "Lead": {
    "Id": "00Q5e00000DEMO1",
    "Email": "priya.natarajan@example.com",
    "Phone": "(415) 555-0142",
    "FirstName": "Priya",
    "LastName": "Natarajan",
    "Street": "500 Howard St., Suite 850",
    "City": "San Francisco",
    "StateCode": "CA",
    "PostalCode": "94105",
    "CountryCode": "US",
    "GCLID__c": "Cj0KCQjw...",
    "Consent_Ad_User_Data__c": "GRANTED",
    "Consent_Ad_Personalization__c": "GRANTED"
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `EventId` | **yes** | Unique per event. This is the idempotency key: send the same `EventId` twice and we process it once. Use the CRM's own event or delivery id, not the lead id — a lead can convert more than once. |
| `EventTime` | **yes** | ISO 8601 **with timezone** (`Z` or `+hh:mm`). This is when the outcome happened, not when you sent it. Attribution windows are computed from it. Naive timestamps are rejected. |
| `Conversion_Action__c` | **yes** | Which conversion this is. Must match a conversion action configured on the platform side; an unknown value is accepted by us and rejected by the platform (section 6). |
| `Amount`, `CurrencyIsoCode` | no | Conversion value. Currency is ISO 4217. |
| `Lead.Id` | **yes** | Your lead id. Not sent to the platform; kept for your reference. |
| `Lead.GCLID__c` | no* | The click id captured on the landing page at form submit. When present, matching uses it and identifiers are supplementary. |
| `Lead.Email`, `Lead.Phone` | no* | Raw values. We normalise and hash them; you do not. |
| `Lead.FirstName`, `Lead.LastName`, `Lead.Street`, `Lead.City`, `Lead.StateCode`, `Lead.PostalCode`, `Lead.CountryCode` | no* | Address block. Use the ISO picklist fields `StateCode` / `CountryCode`, not the free-text `State` / `Country` — `"United States"` is not a country code and will be rejected. |
| `Lead.Consent_Ad_User_Data__c`, `Lead.Consent_Ad_Personalization__c` | see 3.3 | Exactly `GRANTED`, `DENIED` or `UNSPECIFIED`. |

\* At least one **match key** is required: a click id, an email, a phone, or a
full address (first name + last name + postal code + country). A record with
none of these cannot be matched to anyone and is rejected.

Unknown fields are ignored. Blank strings (`""`) are treated as absent.

### 3.2 HubSpot-shaped

```json
{
  "eventId": 987654321,
  "objectId": 7701,
  "occurredAt": 1790069400000,
  "conversionAction": "qualified_lead",
  "properties": {
    "email": "tom.okafor@example.com",
    "phone": "+44 20 7946 0958",
    "firstname": "Tom",
    "lastname": "Okafor",
    "address": "14 Rue de l'Église",
    "city": "Lyon",
    "state": "ARA",
    "zip": "69001",
    "country": "FR",
    "hs_google_click_id": "Cj0KCQjw...",
    "amount": "950.00",
    "deal_currency_code": "EUR",
    "ad_user_data_consent": "GRANTED",
    "ad_personalization_consent": "GRANTED"
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `eventId` | **yes** | Integer. Idempotency key, same rules as `EventId` above. |
| `objectId` | **yes** | Your contact or deal id. |
| `occurredAt` | **yes** | Epoch **milliseconds**, UTC. |
| `conversionAction` | **yes** | As above. |
| `properties.hs_google_click_id` | no* | Click id. |
| `properties.email`, `properties.phone` | no* | Raw values. |
| `properties.firstname`, `lastname`, `address`, `city`, `state`, `zip`, `country` | no* | Address block. `country` must be an ISO 3166-1 alpha-2 code (`FR`, not `France`). |
| `properties.amount`, `deal_currency_code` | no | Amount as a string is fine (`"950.00"`); it is parsed exactly. |
| `properties.ad_user_data_consent`, `ad_personalization_consent` | see 3.3 | `GRANTED` / `DENIED` / `UNSPECIFIED`. |

Same match-key rule as Salesforce.

### 3.3 Consent fields

Two signals, mirroring the platform's consent model:

| Signal | Meaning |
| --- | --- |
| `ad_user_data` | May this person's data be sent to the ad platform for measurement? |
| `ad_personalization` | May it also be used to personalise ads? |

**We upload only when both are exactly `GRANTED`.** Everything else — `DENIED`,
`UNSPECIFIED`, a missing field, a typo like `"granted"` or `"yes"` — results
in the event being recorded as `SUPPRESSED` with a reason that says which
signal blocked it and why:

| You sent | Recorded reason |
| --- | --- |
| `ad_user_data: DENIED` | `ad_user_data_denied` — the user opted out. Expected; nothing to fix. |
| `ad_user_data: UNSPECIFIED` | `ad_user_data_unspecified` — your consent banner did not capture a choice. |
| field absent | `ad_user_data_missing` — your integration is not sending the field at all. **This usually means a tagging bug on your side.** |
| (same three for `ad_personalization`) | |

A value that is not one of the three enum strings is a schema error (422),
not a suppression. We would rather reject a malformed consent signal than
guess what it meant.

Populate these from your consent management platform at form-submit time and
store them on the lead. If consent is granted later, send a new event with a
new `EventId`.

---

## 4. Signing requests

Every request carries two headers:

```
X-Webhook-Timestamp: 1790069400
X-Webhook-Signature: sha256=<hex>
```

Compute the signature as

```
HMAC-SHA256( key = shared_secret,
             message = "<timestamp>" + "." + <raw request body bytes> )
```

hex-encoded, lowercase, prefixed with `sha256=`. The timestamp is the current
Unix time in seconds, as a decimal string, and **it is part of the signed
message**: a request with a modified timestamp fails verification.

Sign the exact bytes you send. Do not re-serialise the JSON after signing;
whitespace or key order changes will break the signature.

**Worked example** (secret `your-shared-secret`, timestamp `1790069400`, body
exactly as below with no whitespace):

```
{"EventId":"e-1001","EventTime":"2026-09-22T09:30:00Z","Conversion_Action__c":"closed_won","Lead":{"Id":"00Q1","Email":"a@example.com","CountryCode":"US","Consent_Ad_User_Data__c":"GRANTED","Consent_Ad_Personalization__c":"GRANTED"}}
```

```
X-Webhook-Signature: sha256=903c541d2cc0e7e3df08d5602df95cec023328d0430de94219e00113e6aaeafc
```

Python reference:

```python
import hashlib, hmac, time

def sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": f"sha256={digest}"}
```

**Timestamp window.** Requests whose timestamp is more than **300 seconds**
from our clock are rejected. Keep your sending host on NTP.

You have a separate secret per source. Rotate by asking us for a new one; we
accept both for a transition window.

---

## 5. Responses

| Status | Body | Meaning | What to do |
| --- | --- | --- | --- |
| **202** | `{"event_id","status":"QUEUED","correlation_id"}` | New event, durably recorded, processing asynchronously. | Nothing. Store `event_id` and `correlation_id` against the lead if you can; they make support conversations short. |
| **200** | same shape | Duplicate: this `EventId` was already recorded. The original `event_id` is returned. Nothing new happened. | Nothing. Safe to retry freely. |
| **422** | `{"event_id","status":"REJECTED","correlation_id","errors":[{"field","reason"}]}` | Authenticated but invalid. Recorded as `REJECTED` and queryable. | Fix the payload and send with a **new** `EventId`. |
| **401** | `{"detail":{"reason":...}}` | Signature or timestamp failed. Nothing recorded. | Check secret, clock, and that you signed the exact bytes sent. |
| **404** | | Unknown source in the path. | |
| **5xx** | | Our side. | Retry with backoff. The idempotency key makes retries safe. |

`event_id` is `<source>:<your EventId>`, e.g. `salesforce:e-1001`.

Retry policy we recommend on your side: retry 5xx and network errors with
exponential backoff for up to an hour. Do not retry 4xx.

---

## 6. What gets rejected, and why

Rejections happen at two points. Both leave a queryable record.

### 6.1 At the webhook (422, immediate)

Schema problems. The `errors` array names each field and a reason code:

| `reason` | Meaning |
| --- | --- |
| `malformed_json` | Body is not JSON. |
| `missing` | A required field is absent. |
| `enum` | A consent field has a value other than `GRANTED` / `DENIED` / `UNSPECIFIED`. |
| `value_error` on `EventTime` | Timestamp has no timezone. |
| `string_too_short`, `int_parsing`, … | Standard type errors on the named field. |

Rejection responses never echo your input values back, so nothing personal
appears in a log or ticket.

### 6.2 During processing (`REJECTED` status, visible via `GET /events`)

Normalisation problems — the values were the right shape but cannot be turned
into something the platform can match on. **We reject rather than send a
best-effort hash**, because a digest of a badly-normalised value looks like a
valid upload and silently matches nothing, which is worse than no upload.

| `status_reason` | Meaning | Fix on your side |
| --- | --- | --- |
| `unparseable:phone` | Not recognisable as a phone number. | Send digits with a country code, or set `CountryCode` / `country` so national formats can be resolved. |
| `invalid:phone` | Parsed, but not a valid number for that country. | Usually a data-entry problem in the CRM. |
| `no_default_region:phone` | National-format number and no usable country on the record. | Send `CountryCode` / `country`. |
| `malformed:email` | Not `local@domain`. | |
| `not_alpha2:country` | Country is not a two-letter ISO code. | Use the code picklist, not the name. |
| `empty:<field>` | Field contained only whitespace or, for names, only titles (`"Dr."`). | |
| `no_match_key:identifiers` | No click id, no email, no phone, no full address. | Send at least one. |

Several fields can fail at once; the reason lists all of them
(`malformed:email;unparseable:phone`).

### 6.3 At the platform (`DEAD_LETTERED` status)

The platform refused the row. Most common: `CONVERSION_ACTION_NOT_FOUND`, which
means the `conversionAction` you sent is not configured on the platform
account. We keep the full record and can replay it once the mapping is fixed —
you do not need to resend.

---

## 7. Checking on an event

```
GET https://<gateway-host>/events/salesforce:e-1001
```

```json
{
  "event_id": "salesforce:e-1001",
  "source": "salesforce",
  "status": "UPLOADED",
  "status_reason": null,
  "received_at": "2026-09-22T09:30:02.114Z",
  "conversion_action": "closed_won",
  "match_key_type": "click_id",
  "attempt_count": 1,
  "transitions": [
    {"from_status": null,        "to_status": "RECEIVED",  "reason": null, "occurred_at": "..."},
    {"from_status": "RECEIVED",  "to_status": "VALIDATED", "reason": null, "occurred_at": "..."},
    {"from_status": "VALIDATED", "to_status": "QUEUED",    "reason": null, "occurred_at": "..."},
    {"from_status": "QUEUED",    "to_status": "PROCESSED", "reason": null, "occurred_at": "..."},
    {"from_status": "PROCESSED", "to_status": "UPLOADING", "reason": null, "occurred_at": "..."},
    {"from_status": "UPLOADING", "to_status": "UPLOADED",  "reason": null, "occurred_at": "..."}
  ]
}
```

| `status` | Meaning |
| --- | --- |
| `QUEUED`, `PROCESSED`, `UPLOADING` | In flight. |
| `UPLOADED` | Accepted by the platform. Done. |
| `SUPPRESSED` | Not uploaded because of consent. `status_reason` says which signal. |
| `REJECTED` | Could not be normalised. `status_reason` lists the fields. Terminal. |
| `FAILED_RETRYABLE` | Platform asked us to try again later; we will. |
| `DEAD_LETTERED` | Platform permanently refused it, or retries were exhausted. We can replay after a fix. |

This endpoint never returns identifiers, hashed or otherwise.

---

## 8. Expected latency

| From | To | Typical | Worst case |
| --- | --- | --- | --- |
| Your request | Our 202 | 20–50 ms | 500 ms |
| 202 | `PROCESSED` (consent + hashing) | 1–2 s | 30 s under backlog |
| `PROCESSED` | `UPLOADED` | 5–10 s (we batch for up to 5 s) | 10–15 min if the platform is rate-limiting us and we are backing off |
| Platform accepts | Conversion visible in platform reporting | Platform-dependent, typically hours | |

If a platform outage lasts longer than our retry ceiling (10 minutes of
backoff), affected events are dead-lettered and replayed by us once the
platform recovers. You will not need to resend.

---

## 9. Checklist before go-live

- [ ] `EventId` is unique per *event*, not per lead
- [ ] `EventTime` / `occurredAt` is the outcome time, with timezone
- [ ] Country is an ISO alpha-2 code on every record
- [ ] Both consent fields are populated from your CMP, with the exact enum strings
- [ ] Click id is captured at form submit and stored on the lead
- [ ] Signature is computed over the exact bytes sent, timestamp included
- [ ] Sending host clock is NTP-synced
- [ ] Your retry logic retries 5xx only and treats 200 as success
- [ ] Someone on your side knows the `GET /events` endpoint exists

Send us three test events (one valid, one with consent denied, one with a
deliberately bad phone number) and we will confirm each landed in the expected
state before switching on production traffic.
