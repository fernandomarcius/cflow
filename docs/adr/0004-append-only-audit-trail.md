# ADR 0004 - Append-only audit trail

Status: accepted

## Decision

Every write records who, what, when and a before/after diff in the `auditoria` table, in the **same
transaction** as the business write. The application database role has only `INSERT` and `SELECT`
on that table; there is no update or delete path in the code. Corrections never rewrite history;
they add new records. Logins, failures and permission changes are audited too.

## Rationale

A mutable trail is not a trail. Atomic recording prevents "the change happened but the audit entry
did not" and the reverse.

## Consequences

- No retention job ever touches the trail; only technical logs (JSON on stdout) are rotated.
- Secrets never enter the diff; sensitive fields are redacted before recording.
