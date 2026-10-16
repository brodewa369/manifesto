# RFC-0001: The `agent.manifest.yaml` job contract

- Status: Accepted
- Version: 0.1.0
- Author: Brodewa
- Updated: 2026-10-17

## 1. Motivation

Autonomous agents now accrue tool-servers and credentials almost by accident: an
MCP server here, an API token there, a cron job somewhere else. Each *enforcer*
(AgentWard, AgentBound, Permit.io, in-house proxies) can govern an agent — but
every one speaks its own policy dialect, and none of them can read the others'.

The result is a governance tower of Babel:

- **No portable declaration.** There is no single, human-readable file that says
  *"this agent may do exactly these things, bounded this way, until this date."*
- **No diff-able record.** Policy lives in vendor dashboards, so you cannot
  review an agent's power in a pull request or `git log` it.
- **No drift detection.** An agent quietly gains a tool-server and nobody
  notices, because there is no baseline to diff against.

**manifesto is the unowned glue layer**: a manifest schema, a validator/linter, a
discovery scanner, and exporters — so *any* enforcer can consume the same
portable contract. It is **explicitly not a runtime proxy**: it never sits in the
request path and never enforces anything itself. Enforcement remains the
enforcer's job; manifesto's job is to be the readable, portable source of truth.

## 2. Design principles

1. **Human-readable, diff-able.** YAML a person can read in a code review.
2. **Default-deny.** Anything not granted is denied. `deny` exists for clarity
   and future "soft-deny" semantics, not because denial is the default path.
3. **Bounded by default where it matters.** Money and shell execution cannot be
   granted "unbounded" — the validator rejects them.
4. **Portable.** No vendor names in the schema; exporters translate outward.
5. **Zero dependencies.** Pure standard library.

## 3. Schema

A manifest is a single YAML mapping. Fields:

| Field         | Type                      | Required | Meaning |
|---------------|---------------------------|----------|---------|
| `agent`       | string                    | **yes**  | Agent name — the contract's subject. |
| `version`     | int / string              | no       | Contract revision. |
| `owner`       | string                    | no       | Human or team accountable. |
| `description` | string                    | no       | One-line purpose. |
| `ttl`         | ISO date (`YYYY-MM-DD`)   | no\*     | When all grants expire. Missing → validator *warning*. |
| `grants`      | map: grant-key → `true` \| object | no | What the agent MAY do. |
| `deny`        | list of grant-keys        | no       | Explicit refusals (clarity / future soft-deny). |
| `audit`       | map                       | no       | `receipt_of: [grant-keys]` — require receipts for these grants. |
| `meta`        | map (free-form)           | no       | Anything else, ignored by the validator. |

\* Recommended; a missing `ttl` is a warning, and `--strict` makes warnings fail.

### 3.1 Grant keys — the verb namespace

Grant keys follow exactly **`<system>.<verb>`** (two lowercase segments):

```text
gmail.read   gmail.send   payments.charge   fs.write   shell.exec
```

Anything that is not exactly two segments (no dots, or more than one) is
*ungroupable* and rejected with a suggestion. The validator carries a registry
of known systems and their verbs; unknown combinations are rejected with a
"did you mean …" hint.

### 3.2 Grant values — bounds

A grant value is either:

- **`true`** — grant it (only allowed for verbs that are safe to grant
  unbounded, e.g. `gmail.read`); **or**
- **a constraint object** with any of:

  | Key           | Shape                            | Purpose |
  |---------------|----------------------------------|---------|
  | `amount`      | `{ max: <num>, currency: <str>, window: <str> }` | Cap money per window. |
  | `rate`        | `{ max: <num>, window: <str> }`  | Cap calls per window. |
  | `to_domains`  | `[ "<domain>", … ]`              | Restrict outbound targets. |
  | `approval`    | `required` \| `readonly-allowed` | Human-approval gate. |

**Bounded-required verbs** (the validator enforces these):

- **Money verbs** (`payments.charge`, `payments.refund`, `payments.payout`) must
  carry a numeric `amount.max` — a bare `true` is *unbounded* and rejected.
- **`shell.exec`** is unbounded by nature, so it must carry `approval: required`.

## 4. Full example

```yaml
agent: sales-ops-bot
version: 1
owner: revenue-team
description: Drafts outreach and charges customers, strictly capped per day.
ttl: 2027-06-30

grants:
  gmail.read: true
  gmail.send: true
  gmail.draft: true
  calendar.read: true
  calendar.create: true
  payments.charge:
    amount:
      max: 500
      currency: USD
      window: P1D
    approval: readonly-allowed

deny:
  - payments.payout
  - shell.exec
  - fs.delete

audit:
  receipt_of:
    - payments.charge

meta:
  team: revenue
  runbook: https://example.internal/runbooks/sales-ops-bot
```

## 5. Interop

The manifest is the **portable source of truth**; `manifesto export` translates
it into an enforcer's dialect:

| Target       | Command                                   | Output |
|--------------|-------------------------------------------|--------|
| AgentWard    | `manifesto export m.yaml --target agentward` | Approximate AgentWard-style policy YAML skeleton. |
| Deny-list    | `manifesto export m.yaml --target denylist`  | Platform-neutral deny-list JSON (`defaultEffect: deny`, sorted). |

> **Interop caveat.** Both outputs are **approximations**, not certified vendor
> schemas. They are reviewable starting points — read the emitted header,
> reconcile with your enforcer's real schema, then enforce. manifesto never
> calls a vendor API and never enforces at runtime.

Exports are **deterministic**: the same manifest always produces the same bytes
(sorted keys, no timestamps), so exports are diff-able and safe to commit.

## 6. Changelog

### 0.1.0 — 2026-10-17
- Initial release.
- Schema: `agent`, `version`, `owner`, `description`, `ttl`, `grants`, `deny`,
  `audit`, `meta`.
- `<system>.<verb>` namespace with a known-systems registry and suggestions.
- Bounded-required enforcement for money verbs and `shell.exec`.
- Hand-rolled validator (no `jsonschema`): unknown keys, unknown/ungroupable
  verbs, unbounded grants, empty grants, shadowed grants, missing/expired ttl,
  audit-of-non-granted-key — each with key-path, message, and fix hint.
- Read-only discovery scanner for hermes/claude/cursor/vscode + crontab + git
  user, with provenance and "no manifest on file" findings.
- Deterministic exporters (AgentWard-style, deny-list JSON).
- Zero runtime dependencies.
