# RFC-0001: The `agent.manifest.yaml` job contract

- Status: Accepted
- Version: 0.2.0
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
| `delegations` | list of delegation objects | no      | Attenuated sub-agent grants (see §3.3). |
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

### 3.3 Delegation — attenuation only (RFC-0001 § Delegation)

An agent may hand a **narrowed slice** of its power to a sub-agent. A
delegation is pure **attenuation**: it may only ever *reduce* what the
delegating agent itself holds — never widen it. Each entry in `delegations:`:

| Field          | Type            | Required | Meaning |
|----------------|-----------------|----------|---------|
| `from`         | string          | **yes**  | The delegating agent. Must equal this contract's `agent` so the parent's grant set is resolvable. |
| `to`           | string          | **yes**  | The receiving sub-agent. |
| `narrower_than`| map (grant-key → `true` \\| object) | **yes** | The child's effective grant set — must be a *subset*, narrowed. |
| `purpose`      | string          | **yes**  | Why the delegation exists. Missing → validator *error* (`delegation_missing_purpose`). |
| `ttl`          | ISO date        | no       | When the delegation lapses. |

The validator enforces attenuation (`delegation_escalation` on violation):

1. **Key inclusion** — every grant in `narrower_than` must already be granted
   to the delegating agent (`from`). Delegating a verb the parent lacks is
   escalation.
2. **Per-verb narrowing** — for each shared verb, the child's constraint object
   must be *narrower than or equal to* the parent's:
   - a parent cap (`amount.max`, `rate.max`) must be carried by the child at a
     value **≤** the parent's — dropping the cap, or raising it, is escalation;
   - a parent `to_domains` must be narrowed to a **subset**;
   - a parent `approval` gate must be kept at **equal or greater** strictness
     (`required` ⊒ `readonly-allowed`).

   The tricky, deliberate consequence: if the parent holds a verb **only via a
   constraint object** (e.g. `payments.charge` capped at $500/day), a child
   that tries to take that verb as a bare `true` (unbounded) is **rejected** —
   a child can never hold a power the parent holds only under constraint.

A contract with no escalations explains as an attenuation chain, e.g.
`manifesto explain`:

```text
delegations:
  sales-lead -> sales-ops (narrowed: send email at most 20 per P1D, charge money up to 50 USD per P1D; purpose: coverage handoff)
```

**Interop note.** This attenuation model is deliberately inspired by **Macaroon**
and **Biscuit** caveat/discharge semantics: a root caveat set that a downstream
token may only *narrow*, never discharge into more authority. manifesto does not
implement a token format — it is a *declarative* check that the same narrowing
discipline holds at the contract level, so any enforcer that speaks
Macaroon/Biscuit-style attenuation can consume the exported, provably-narrower
delegation set directly.

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
| Cedar        | `manifesto export m.yaml --target cedar`     | Cedar policy set: `permit`/`forbid` statements plus mapping comments. |

> **Interop caveat.** All three outputs are **approximations**, not certified
> vendor schemas. They are reviewable starting points — read the emitted header,
> reconcile with your enforcer's real schema, then enforce. manifesto never
> calls a vendor API and never enforces at runtime.

Exports are **deterministic**: the same manifest always produces the same bytes
(sorted keys, no timestamps), so exports are diff-able and safe to commit.

### 5.1 Cedar mapping table

`--target cedar` emits a Cedar **policy set** (a file of top-level
`permit`/`forbid` statements, plus `//` comments). Manifest construct → Cedar
construct, with the fidelity of each mapping marked:

| Manifest construct | Cedar construct | Fidelity |
|--------------------|-----------------|----------|
| `agent` | `principal == Agent::"<agent>"` on every statement | **APPROXIMATION** — no schema or entity store is emitted; you must declare the `Agent` type (and `Action`) in your Cedar schema. |
| grant key `<system>.<verb>` | `action == Action::"<system>.<verb>"` | **Verbatim** — the action id is preserved byte-for-byte, so your schema maps `Action` ids straight from the manifest. |
| grant value `true` | bare `permit(...)` with no condition | **Exact.** |
| `amount: { max, currency, window }` | `when { context.amount_max == … && context.amount_currency == … && context.amount_window == … }` | **APPROXIMATION** — Cedar has no money/window primitive; the cap becomes a `context.*` predicate the enforcer must populate and enforce. |
| `rate: { max, window }` | `when { context.rate_max == … && context.rate_window == … }` | **APPROXIMATION** — same: rate limiting lives in the enforcer, not in Cedar. |
| `to_domains: [ … ]` | `when { (context.to_domain == "a" \|\| context.to_domain == "b") }` | **APPROXIMATION** — request-time target resolution is external. |
| `approval: required \| readonly-allowed` | `when { context.approval == "…" }` | **APPROXIMATION** — approval state arrives in the request context. |
| `deny: [ <key> ]` | `forbid(principal …, action == Action::"<key>", resource)` | **Semantically aligned** — Cedar's `forbid` always beats `permit`, matching manifesto's "deny dominates" (`shadowed_grant`) rule. Default-deny matches Cedar's native implicit deny. |
| `deny: [ <system> ]` (namespace) | comment only: *enumerate `<system>.*` as `forbid`* | **APPROXIMATION** — Cedar has no namespace-wide action id. |
| `delegations[].narrower_than` | an extra `permit(...)` for the child principal (`Agent::"<to>"`), annotated with `// delegation: <from> -> <to>` | **APPROXIMATION** — no principal hierarchy or attenuated-transfer semantics; the child permit simply restates the narrowed constraints as `context.*` predicates. |
| `ttl` | `// ttl: …` comment only | **APPROXIMATION** — contract expiry is enforced outside Cedar (token/contract layer), not as a policy condition. |
| `audit.receipt_of` | `// audit.receipt_of: …` comment only | **APPROXIMATION** — Cedar has no receipt primitive. |
| default-deny | *nothing emitted* — relies on Cedar's implicit deny | **Native Cedar behaviour.** |

> **Honest caveat.** The Cedar policy overrides above are **APPROXIMATIONS
> validated byte-stable** (golden-tested like the other dialects) **and
> reviewed manually — they are *not* vendor-certified**, not validated against
> any particular Cedar engine's schema, and not a claim that the emitted
> `context.*` predicates are enforced anywhere. Cedar is used here as a widely
> readable policy language: the export shows *what* would be permitted and
> *which* bounds must hold, so a human can port it into a real Cedar schema.
> `//` comments in the emitted file carry this same warning.

### 5.2 Reading a Cedar export

Excerpt of the real output for `examples/sales-ops-bot.yaml` (`...` marks
elided statements; the committed bytes live in
`fixtures/golden/sales-ops-bot.cedar.expected`):

```text
$ manifesto export examples/sales-ops-bot.yaml --target cedar
// manifesto export -> cedar (APPROXIMATION)
// generated by manifesto 0.3.0 | dialects are approximations, not certified vendor schemas — review before enforcing
...                                              <- mapping table + caveat as comments
permit(
  principal == Agent::"sales-ops-bot",
  action == Action::"payments.charge",
  resource
)
when {
  context.amount_max == 500
  && context.amount_currency == "USD"
  && context.amount_window == "P1D"
  && context.approval == "readonly-allowed"
};
forbid(
  principal == Agent::"sales-ops-bot",
  action == Action::"fs.delete",
  resource
);
...                                              <- payments.payout, shell.exec forbids
// audit.receipt_of: payments.charge (APPROXIMATION: Cedar has no receipt primitive — ...)
```

## 6. Contract diff — `manifesto diff`

> *"This PR raises the payments cap from $500 to $1000."* A contract that only
> ever grows is a governance bug you find out about in an incident, not in
> review. `manifesto diff` turns that into a reviewable table with an exit code.

```bash
manifesto diff <old.yaml> <new.yaml> [--json] [--strict] [--allow-loosening]
```

Typical use — gating an agent's job contract in CI or in a PR:

```bash
manifesto diff main/agent.manifest.yaml pr/agent.manifest.yaml --strict
```

### 6.1 What is reported

Every change is a row: **area**, **change**, **detail**, plus a **direction**.

| Area group | Areas | Change kinds |
|------------|-------|--------------|
| `grant` | each grant key | `grant added`, `grant removed`, `grant changed` |
| `deny` | `deny:<entry>` | `deny added`, `deny removed` |
| `expiry` | `ttl` | `expiry added`, `expiry removed`, `expiry changed` |
| `delegation` | `delegations:<from>-><to>` | `delegation added`, `delegation removed`, `delegation changed` |
| `audit` | `audit.receipt_of:<key>` | `receipt added`, `receipt dropped` |

**Direction** — how power moved:

| Direction | Examples |
|-----------|----------|
| **tightened** | `amount.max` lowered, `rate.max` lowered, `to_domains` narrowed, an approval gate added or made stricter (`required` ⊒ `readonly-allowed`), constraints added over a bare `true`, a grant/delegation/receipt dropped, a `deny:` entry added, expiry moved earlier. |
| **loosened** | `amount.max` raised, `rate.max` raised, `to_domains` widened, an approval gate dropped or weakened, constraints dropped (now unbounded), a grant/delegation added, a `deny:` entry removed, a receipt requirement dropped, expiry moved later or removed. |
| **neutral** | reported, direction unknown — e.g. a `currency`/`window` change (a shorter amount *window* can mean a larger allowance per day, so direction depends on the enforcer's period semantics), a mixed target re-set. |

Ties produce **no row**: two manifests with the same caps, domains, gates,
delegations and expiry diff to *no changes*. Where a single grant moves both
ways (rate lowered, cap raised) the row is **loosened** — worst case wins.

The summary line counts grants added/removed/tightened/loosened, deny changes,
expiry changes, delegation changes and audit changes. Output is deterministic:
the same pair of files always renders the same bytes, and `--json` emits the
same report as `changes[]` + `summary` + `gate`.

### 6.2 The loosening gate

A change is **gated** when it is classified **loosened** *and* it touches

- a **sensitive class** — `payments`, `fs` (and its `files` alias), `shell`; or
- a **previously explicitly-denied** key or namespace (anything listed in the
  baseline's `deny:` — including a grant of a key that deny covered).

`--strict` turns a gated loosening into **exit 1**; `--allow-loosening`
explicitly accepts it (**exit 0**) after the reviewer has looked at the table.
Everything is still printed in both cases — the gate changes the exit code, not
the report.

Loosening *outside* those areas (raising a `gmail.send` rate, say) is reported
but does not fail `--strict`; it is not an area the gate claims to own. Run
`diff` without `--strict` to review without gating.

| Exit code | Meaning |
|---|---|
| `0` | no gated loosening (or `--allow-loosening` was passed) |
| `1` | gated loosening under `--strict` |
| `2` | usage error or unreadable manifest |

manifesto does **not** rewrite or merge manifests — it only reports. Deciding
whether a loosening is intended stays a human act.

## 7. Org verb-registry override — `--registry`

The built-in registry (`KNOWN` in `src/manifesto/schema.py`) is **data**, not
code — and it is *generic*, not yours. An organisation that owns verbs of its
own, or that wants a stricter house rule, layers a **registry file** over it:

```bash
manifesto validate agent.manifest.yaml --registry our_verbs.yaml
```

```yaml
registry: acme-corp            # label, echoed in output and --json
mode: extend                   # extend (default) | replace
unknown_verb: warning          # error (default) | warning | ignore
systems:                       # the org's own <system> -> verbs
  acme: [deploy, rollback, status]
risk_classes:                  # <system>.<verb> -> org risk class
  acme.deploy: critical
  payments.charge: high
require_approval:              # risk classes that must be gated
  - critical
```

| Key | Meaning |
|-----|---------|
| `mode: extend` | org systems are **added** to the built-ins — `gmail.read` still validates, and `acme.deploy` now does too. |
| `mode: replace` | the file is the **only** authority: anything not declared in it (built-ins included) is an unknown verb. For orgs with a closed vocabulary. |
| `unknown_verb:` | what happens to a verb outside the effective registry: `error` (unchanged default), `warning` (reported, fails only under `--strict`), `ignore` (suppressed entirely). |
| `risk_classes:` | maps a grant key to an org risk class. The class itself is reported back through `validate --json`; it becomes a *rule* via `require_approval`. |
| `require_approval:` | a listed risk class must be granted with `approval: required`, otherwise the validator emits **`missing_approval`** (error). `readonly-allowed` does **not** satisfy it — that is the point of the gate. |

Behaviours worth knowing:

- **A registry is authority, so it is parsed strictly.** An unreadable file, a
  non-mapping document, an unknown top-level key, a bad `mode`/`unknown_verb`
  value, a `systems` entry that is not a verb list, or a `risk_classes` key
  naming a verb the registry itself does not know all raise a hard error
  (`manifesto validate` exits `2`). Nothing is skipped silently — a typo in an
  authority file must never downgrade a manifest to "looks fine".
- **Risk classes are validated against the effective registry**, so
  `risk_classes: { gmail.sendit: high }` fails at load time with a "did you
  mean" hint instead of quietly gating nothing.
- **The override is opt-in and scoped.** Without `--registry` the validator is
  byte-identical to before (existing tests assert this); `--registry` affects
  `validate` only — `scan`, `explain` and `export` read the manifest as-is.
- `validate --json` gains a `registry` block (`label`, `mode`, `unknown_verb`,
  `systems`, `risk_classes` count, `require_approval`, `source`) when a
  registry is active; the human output prints a `registry:` banner line.

See `fixtures/registry/acme.yaml` (the org registry) and
`fixtures/registry/acme-deploy-bot.yaml` (a contract in the org vocabulary that
fails without it and passes with it).

## 8. Changelog

### 0.2.0 — 2026-10-17
- **Formal machine-readable schema** published at
  `schema/agent-manifest.schema.json` (JSON Schema draft-07 subset: types,
  `required`, `enum`, `pattern`, `propertyNames`, `additionalProperties`,
  local `$ref`). The validator's **structural** layer is now *derived from this
  file* via a tiny embedded draft-07-subset engine (`manifesto.jsonschema_mini`)
  — no `jsonschema` dependency. A canary test mutates the schema (e.g. makes
  `ttl` required) and proves validator behaviour follows the file.
- **Scanner is fail-closed.** Inference happens only on an *exact* match against
  an explicit server table; an unrecognized tool-server or an unparseable config
  yields `unknown: true`, never a guessed grant. Every inferred grant is
  annotated `<file>:<line>`. New findings: `type-suspect`, `credential-orphan`,
  `expired-ttl`, `unbounded-grant`.
- **Delegation attenuation** (`delegations:`): a child's `narrower_than` grant
  set must be a narrowed subset of the parent's (`from`), with `purpose`
  required. Escalation — including taking a verb the parent holds only via a
  constraint as a bare `true` — is rejected. `explain` renders attenuation
  chains; the model is inspired by Macaroon/Biscuit caveat semantics (§3.3).
- **Golden-file export suite** (`fixtures/golden/*.expected`) byte-compares both
  dialects for determinism; labeled APPROXIMATION with justification in
  `fixtures/MANIFEST.md`.
- Exporters now carry `delegations` deterministically.

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
