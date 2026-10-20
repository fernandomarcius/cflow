# ADR 0001 - Self-hosted first, air-gapped capable

Status: accepted

## Context

cFlow processes data that is often sensitive (financial, operational, personal). Operators need to
keep the data plane and the control plane inside their own infrastructure, and some of them run in
networks with no outbound access.

## Decision

Every component runs on infrastructure you operate: PostgreSQL (metadata), Temporal (orchestration),
the API, the worker and an object store or a local directory (lake). Nothing "phones home". Optional
cloud integrations (for example a GCS bucket as the lake backend) are **configuration**, never a
hard dependency: switching the backend to `local` must keep the platform working.

## Rationale

- Data sovereignty and auditability are product requirements.
- A SaaS dependency in the critical path adds availability and leakage risks.
- Pluggable backends keep the door open to cloud storage without coupling the code to a vendor SDK.

## Consequences

- Operating PostgreSQL, Temporal and the object store is the operator's responsibility.
- Credentials of external systems live in the platform vault, never in environment files or code.
- Only permissively licensed dependencies (MIT/BSD/Apache-2.0) are embedded in the code base.
