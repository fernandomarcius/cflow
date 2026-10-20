# ADR 0010 - Ingestion: one connector per access class, one declarative contract per source

Status: accepted

## Decision

Connectors are per **access class**, not per source: `http_file` (file by URL convention with
`ETag`/`If-Modified-Since`), `rest_api` (paginated JSON by window), `odata` (`$top`/`$skip`) and
`portal_download`. A source is a **declarative JSON contract**: access class, URL pattern, format,
**encoding**, delimiter, expected schema, cadence, partition key, window policy and grain. A
connector exposes two verbs, `descobrir(window) -> [Request]` and `baixar(Request) -> Payload`, and
does not know which source it serves.

## Rationale

- Adding a source is registering a contract, not deploying code.
- Encoding belongs to the contract: decoding by guess corrupts accents silently.
- Each step is a Temporal activity (retry, backoff, replay) and landing -> bronze normalisation is
  DuckDB, on a dedicated task queue.

## Consequences

- Idempotence by payload hash: re-running a slice with the same bytes is a no-op; the landing is
  immutable and the bronze can be rebuilt from it (replay) without touching the network.
- A failing slice goes to quarantine without advancing the watermark; infrastructure failures are
  not quarantine.
- What does not fit the four classes needs a new class: an explicit decision, not an `if` in a
  connector.
