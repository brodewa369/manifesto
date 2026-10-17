<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/license-MIT-green" />
  <img src="https://img.shields.io/badge/dependencies-zero-brightgreen" />
  <img src="https://img.shields.io/badge/agent%20governance-portable-orange" />
</p>

# manifesto

**Portable AI agent job contracts — a manifest schema, validator/linter, discovery scanner, and exporters so any enforcer can consume the same readable spec.**

```text
$ manifesto validate examples/sales-ops-bot.yaml
== manifesto validate | examples/sales-ops-bot.yaml
  no findings
== 0 error(s), 0 warning(s)
$ echo $?
0
```

## The problem it was built for

Agent setups kept accruing tool-servers and credentials with no portable
declaration of what the agent was actually allowed to do. Every governance tool
spoke its own dialect, so policy could not be reviewed in a diff, logged in git,
or checked for drift. **manifesto** is the unowned glue layer: one readable
`agent.manifest.yaml` contract that a validator checks, a scanner discovers, and
exporters translate into whichever enforcer you run. It is **not** a runtime
proxy — enforcement stays with the enforcer.

## Features

- **One portable contract** — `agent.manifest.yaml` is human-readable, diff-able, and enforcer-agnostic. See [SPEC.md](SPEC.md).
- **Formal machine-readable schema** — a published JSON Schema draft-07 subset at [`schema/agent-manifest.schema.json`](schema/agent-manifest.schema.json). The validator's **structural layer is derived from this file** via a tiny embedded engine (`manifesto/jsonschema_mini.py`) — mutate the schema and the behaviour follows (proven by a canary test). No `jsonschema` dependency.
- **Two-layer validator** — structural checks (types, required, enums, key pattern, unknown keys) come from the schema; semantic checks (unknown/ungroupable verbs with "did you mean", unbounded grants, empty grants, shadowed grants, expired/missing ttl, audit cross-refs, delegation attenuation) come from the registry and business rules — each with a key-path and a fix hint.
- **Bounded where it matters** — money verbs (`payments.charge/refund/payout`) require `amount.max`; `shell.exec` requires `approval: required`. Unbounded grants are rejected.
- **Delegation attenuation** — `delegations:` hand a *narrowed slice* of an agent's power to a sub-agent; the child's `narrower_than` set must be a provable subset of the parent's, `purpose` required, escalation rejected (inspired by Macaroon/Biscuit caveats). `explain` renders the attenuation chain.
- **Fail-closed discovery scanner** — parses hermes/claude/cursor/vscode MCP configs, crontab, and git identity; emits DRAFT manifests with `<file>:<line>` provenance. Inference happens **only on an exact server-table match** — an unknown tool-server or an unparseable config yields `unknown: true`, never a guess. Findings: `type-suspect`, `credential-orphan`, `expired-ttl`, `unbounded-grant`, `no_manifest`. Fixture-testable; never touches your real configs in tests.
- **Deterministic exporters** — three dialects: AgentWard-style policy YAML, a platform-neutral deny-list JSON, and a **Cedar policy set** (`permit`/`forbid` + mapping comments); same input always produces the same bytes. A golden-file suite (`fixtures/golden/`) byte-compares every dialect (labeled APPROXIMATION in [`fixtures/MANIFEST.md`](fixtures/MANIFEST.md)).
- **`manifesto diff`** — compare two contracts and see which grants were **added / removed / tightened / loosened**, what happened to the expiry, and which delegations changed — as a human table or `--json`. `--strict` exits **1** on a loosening in a sensitive class (`payments`, `fs`, `shell`) or a previously-denied area; `--allow-loosening` accepts it explicitly. Built for "this PR removes the payments cap — flagged".
- **Org verb-registry override** — `validate --registry our_verbs.yaml` layers your own `<system>.<verb>` vocabulary over the built-ins (`extend` or `replace`), sets the rule for unknown verbs (`error`/`warning`/`ignore`), and maps verbs to **risk classes** whose gated members must carry `approval: required` (`missing_approval`).
- **Plain-language `explain`** — turns a contract into a sentence with risk badges (`unbounded`, `expired`, `dominated`) and delegation chains.
- **Zero dependencies** — pure stdlib. CI/cron-friendly exit codes (`0` clean, `1` findings, `2` usage) with `--strict` for gating warnings.

## Schema overview

```yaml
agent: sales-ops-bot          # required — the contract's subject
version: 1
owner: revenue-team
description: Drafts outreach and charges customers.
ttl: 2027-06-30               # grants expire on this ISO date

grants:                       # anything not granted is DENIED by default
  gmail.read: true            # bounded-free verb -> bare true is fine
  payments.charge:            # money verb -> must carry amount.max
    amount: { max: 500, currency: USD, window: P1D }
    approval: readonly-allowed
  shell.exec:                 # unbounded by nature -> requires approval gate
    approval: required
    rate: { max: 20, window: PT1H }

deny:                         # explicit refusals (clarity / future soft-deny)
  - payments.payout
  - fs.delete

delegations:                  # ATTENUATION only: hand a narrowed slice to a sub-agent
  - from: sales-ops-bot       # must equal `agent` so the parent's grants are checkable
    to: sales-ops
    narrower_than:            # a provable SUBSET of the parent's grants
      gmail.send: { rate: { max: 20, window: P1D } }
    purpose: coverage handoff # REQUIRED — missing -> error
    ttl: 2027-06-30

audit:
  receipt_of: [payments.charge]

meta: { team: revenue }       # free-form, ignored by the validator
```

Grant keys follow exactly `<system>.<verb>` (e.g. `gmail.read`, `payments.charge`, `fs.write`, `shell.exec`). Full field reference and rationale live in [SPEC.md](SPEC.md).

## Pipeline

```mermaid
flowchart TD
    A[agent.manifest.yaml] --> B[mini-YAML load]
    B --> C{validator}
    C -->|errors| D[exit 1 + fix hints]
    C -->|warnings only| E[exit 0, or 1 with --strict]
    C -->|clean| F[exit 0]
    B --> R[org verb registry --registry]
    R --> C
    B --> G[explain: plain language + badges]
    B --> H{export target}
    H -->|agentward| I[approximate policy YAML]
    H -->|denylist| J[deny-list JSON]
    H -->|cedar| K2[Cedar permit/forbid set]
    K[MCP client configs] --> L[scan: read-only discovery]
    L --> M[DRAFT manifests + provenance]
    L --> N[no-manifest finding]
    O[old contract] --> DF{diff}
    A --> DF
    DF -->|gated loosening + --strict| DG[exit 1 + gate table]
    DF -->|otherwise| DH[exit 0 + table]
```

## Format matrix

| Client | Config path (relative to home) | Format | Servers key | Grant inference |
|---|---|---|---|---|
| Hermes Agent | `.hermes/config.yaml` | YAML | `mcp_servers` | per server-name table |
| Claude Desktop | `.claude/claude_desktop_config.json` | JSON | `mcpServers` | per server-name table |
| Cursor | `.cursor/mcp.json` | JSON | `mcpServers` | per server-name table |
| VS Code | `.config/Code/User/mcp.json` | JSON | `servers` | per server-name table |

The scanner also reads `crontab.txt` under the scanned home (or `crontab -l` when scanning a real home) and the git `user.name` / `user.email`.

Grant inference is **fail-closed**: a tool-server is mapped to `<system>.<verb>` grants *only* on an **exact** match against the explicit server table. An unrecognized tool-server — or a config that cannot be parsed — is recorded as `unknown: true`, never a guessed capability, and is surfaced for review.

## Usage

```bash
# Lint a contract (add --json for machines, --strict to fail warnings in CI)
manifesto validate agent.manifest.yaml
manifesto validate --strict --json agent.manifest.yaml

# ...layered over your org's own verb registry (see "Org verb registry" below)
manifesto validate agent.manifest.yaml --registry our_verbs.yaml

# Read-only discovery of running tool-servers
manifesto scan                       # scan the real home
manifesto scan --client hermes       # one client only
manifesto scan --home fixtures/home  # point at a fixture dir (tests use this)

# Plain-language summary + risk badges + delegation chains
manifesto explain examples/coding-agent.yaml
manifesto explain examples/delegating-lead.yaml   # shows the attenuation chain

# Export an enforcer dialect (approximations — review before enforcing)
manifesto export examples/sales-ops-bot.yaml --target agentward
manifesto export examples/sales-ops-bot.yaml --target denylist -o deny.json
manifesto export examples/sales-ops-bot.yaml --target cedar -o policy.cedar

# Review a contract change (CI/PR gate on loosening)
manifesto diff main/agent.manifest.yaml pr/agent.manifest.yaml --strict
manifesto diff before.yaml after.yaml --json
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | clean / valid (warnings allowed unless `--strict`); a `diff` with no gated loosening |
| `1` | findings — validator errors (or warnings under `--strict`); scan "no-manifest" findings; a **gated loosening** under `diff --strict` (unless `--allow-loosening`) |
| `2` | usage error, unreadable manifest, or unreadable/unparseable `--registry` file |

## Cedar export

`--target cedar` emits a **Cedar policy set** — top-level `permit`/`forbid`
statements with the manifest→Cedar mapping written into the file as comments.
Action ids survive verbatim (`Action::"payments.charge"`); bounds become
`when { context.* }` predicates the enforcer must supply. Recorded output
(`...` marks elisions — every shown line is literal):

```text
$ manifesto export examples/sales-ops-bot.yaml --target cedar
// manifesto export -> cedar (APPROXIMATION)
// generated by manifesto 0.3.0 | dialects are approximations, not certified vendor schemas — review before enforcing
//
// contract: sales-ops-bot | version: 1 | owner: revenue-team
...                                              <- description, ttl, mapping table, caveat
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
...                                              <- remaining forbids + audit comment
```

Every example has a committed golden (`fixtures/golden/<name>.cedar.expected`),
so the Cedar bytes are byte-stable exactly like the other two dialects.

> **Honest caveat.** The Cedar overrides are **approximations validated
> byte-stable and reviewed manually — not vendor-certified**, not checked
> against any Cedar engine's schema, and not a claim that the `context.*`
> predicates are enforced anywhere. The full mapping table with per-row
> fidelity marks (verbatim / exact / approximation) is in
> [SPEC.md §5.1](SPEC.md).

## Reviewing a contract change — `manifesto diff`

*"This PR raises the payments cap from $500 to $1000."* Run `diff` against the
baseline in CI and the gate answers for you. Recorded output — the loosening
case, exit **1**:

```text
$ manifesto diff fixtures/diff/before.yaml fixtures/diff/after-loosened.yaml --strict
== manifesto diff | fixtures/diff/before.yaml -> fixtures/diff/after-loosened.yaml
AREA             CHANGE          DETAIL
gmail.send       grant changed   rate.max 20 -> 500
payments.charge  grant changed   amount.max 500 -> 1000
shell.exec       grant added     new grant (approval=required)
deny:shell.exec  deny removed    explicit refusal removed
ttl              expiry changed  2027-06-30 -> 2028-06-30 (later)
== 1 grant(s) added, 0 removed, 0 tightened, 3 loosened; 0 deny added, 1 deny removed; 1 expiry changed, 0 delegation(s) changed, 0 audit changed; 5 change(s) total
!! gate: 3 loosening(s) in a sensitive class (payments/fs/shell) or a previously-denied area:
   - [payments] payments.charge: amount.max 500 -> 1000
   - [shell] shell.exec: new grant (approval=required)
   - [shell] deny:shell.exec: explicit refusal removed
!! GATE (--strict): gated loosening -> exit 1 (re-run with --allow-loosening to accept it)
$ echo $?
1
```

The same pair of files when the new contract is *tighter* — exit **0**:

```text
$ manifesto diff fixtures/diff/before.yaml fixtures/diff/after-tightened.yaml --strict
== manifesto diff | fixtures/diff/before.yaml -> fixtures/diff/after-tightened.yaml
AREA             CHANGE          DETAIL
gmail.read       grant removed   grant dropped (was unbounded)
gmail.send       grant changed   rate.max 20 -> 10
payments.charge  grant changed   amount.max 500 -> 250; approval readonly-allowed -> required
deny:fs.delete   deny added      explicit refusal added
ttl              expiry changed  2027-06-30 -> 2026-12-31 (earlier)
== 0 grant(s) added, 1 removed, 3 tightened, 0 loosened; 1 deny added, 0 deny removed; 1 expiry changed, 0 delegation(s) changed, 0 audit changed; 5 change(s) total
$ echo $?
0
```

Note the `gmail.send` rate increase: it is *reported* as a loosening but is
not in a sensitive class, so it does not trip the gate by itself — the gate
owns `payments`, `fs` (and `files`) and `shell`, plus anything the baseline
explicitly denied. Full rules: [SPEC.md §6](SPEC.md).

## Org verb registry — `validate --registry`

The built-in verb registry is generic data. An org can layer its own
vocabulary, unknown-verb rule and risk classes over it. Recorded output — a
contract written in an org vocabulary fails on its own, then passes with the
registry:

```text
$ manifesto validate fixtures/registry/acme-deploy-bot.yaml
== manifesto validate | fixtures/registry/acme-deploy-bot.yaml
  ERROR   deny[0]: unknown system 'acme' (verb 'rollback')
          fix: did you mean one of: payments?
  ERROR   grants.acme.deploy: unknown system 'acme' (verb 'deploy')
          fix: did you mean one of: payments?
  ERROR   grants.acme.status: unknown system 'acme' (verb 'status')
          fix: did you mean one of: payments?
== 3 error(s), 0 warning(s)
$ echo $?
1

$ manifesto validate fixtures/registry/acme-deploy-bot.yaml --registry fixtures/registry/acme.yaml
== manifesto validate | fixtures/registry/acme-deploy-bot.yaml
   registry: acme-corp (mode: extend, unknown_verb: warning)
  no findings
== 0 error(s), 0 warning(s)
$ echo $?
0
```

And a **risk class** in that registry making a rule — `gmail.send` is
`critical`, `critical` is gated, so a bare grant is rejected:

```text
$ manifesto validate examples/sales-ops-bot.yaml --registry fixtures/registry/acme.yaml
== manifesto validate | examples/sales-ops-bot.yaml
   registry: acme-corp (mode: extend, unknown_verb: warning)
  ERROR   grants.gmail.send: grant 'gmail.send' is risk class 'critical' which the org registry gates with `approval: required`
          fix: add `approval: required`, or move the class out of the registry's `require_approval` list
== 1 error(s), 0 warning(s)
$ echo $?
1
```

Registry files are parsed strictly: a bad `mode`, an unknown top-level key, or
a risk class naming a verb the registry does not know is a hard error
(exit `2`), never a silent skip — a typo in an authority file must not make a
manifest look fine. File format: [SPEC.md §7](SPEC.md).

## Install

```bash
pip install .          # installs the `manifesto` command
# or run straight from source:
python -m manifesto.cli --help
```

On Arch/CachyOS-style systems with an external-managed python (PEP 668), `pip`
refuses to install into the system interpreter — use a venv or pipx:

```bash
python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/manifesto --help
# or
pipx install .
```

Nothing is ever modified: the scanner is read-only, and exporters only write the
file you explicitly name with `-o`.

## Testing

Zero third-party deps — plain `unittest` (160 tests):

```bash
python -m unittest discover -s tests -t .
```

The matrix covers the validator's failure classes (unknown key, unknown verb,
ungroupable verb, unbounded payment, unbounded shell, empty grant, shadowed
grant, expired/missing ttl), the **schema-derivation canary**
(`tests/test_schema_canary.py` — mutates the schema file and proves the
validator follows), **delegation attenuation** including escalation rejections
(`tests/test_delegation.py`), the **fail-closed scanner** against
`fixtures/home/` and `fixtures/unknown-home/` (never the real home), and export
determinism (same input → same bytes, byte-compared to `fixtures/golden/` —
all three dialects, Cedar included).

Added in 0.3.0: `tests/test_diff.py` (classification, tie-cases, expiry,
delegations, the loosening gate, `--strict`/`--allow-loosening` exit codes,
determinism) and `tests/test_registry.py` (extend/replace modes, the three
`unknown_verb` rules, the risk-class approval rule, strict parsing of a
malformed authority file, CLI exit codes).

`examples/` holds four runnable contracts — `sales-ops-bot.yaml`,
`coding-agent.yaml`, `personal-assistant.yaml`, and `delegating-lead.yaml`
(a valid delegation) — all validated in the test suite. `fixtures/diff/` holds
the baseline/tightened/loosened trio behind the `diff` demo, and
`fixtures/registry/` the org registry behind the `--registry` demo; both are
documented in [`fixtures/MANIFEST.md`](fixtures/MANIFEST.md).

## Troubleshooting cookbook

| Finding / hint | What it means | What to do |
|---|---|---|
| `unknown_verb 'x' for system 's'` | The verb isn't in the registry for that system | Take the suggested verb, or extend `KNOWN` in `schema.py` |
| `ungroupable_verb` | Key isn't exactly `<system>.<verb>` | Rewrite as two segments, e.g. `gmail.read` |
| `unbounded_grant` on a money verb | `payments.*` granted without `amount.max` | Add `amount: { max, currency, window }` |
| `unbounded_grant` on `shell.exec` | Shell granted unrestricted | Add `approval: required` (and optionally `rate`) |
| `empty_grant` | Grant is `{}` — grants nothing, hides intent | Write `true` or add real constraints |
| `shadowed_grant` | A deny covers this grant (parent or exact) | Remove the deny, or drop the grant |
| `missing_ttl` (warning) | Grants never lapse on their own | Add `ttl: YYYY-MM-DD`; use `--strict` to force it |
| `no_manifest` (scan) | Tool-servers/credentials with no contract on file | Save the emitted draft as `manifests/<client>.manifest.yaml` |
| `audit_unknown_grant` (warning) | `audit.receipt_of` names a non-granted key | Reference an existing grant key |
| `delegation_escalation` | A delegation would widen (not narrow) the parent's power | Tighten the child's constraints to ≤ the parent's, or drop the grant |
| `delegation_missing_purpose` | A delegation has no `purpose` | Add `purpose: <why>` to the delegation |
| `unknown_parent` | Delegation `from` ≠ this contract's agent | Set `from` to this contract's `agent` so attenuation is checkable |
| `missing_approval` (registry) | A risk class the org gates was granted without `approval: required` | Add `approval: required`, or move the class out of the registry's `require_approval` |
| `unknown_verb` only under `--registry` | The key is outside the org's effective registry | Declare it under `systems:`, fix the key, or set `unknown_verb: warning\|ignore` in the registry |
| `!! GATE (--strict)` (diff) | A loosening touched `payments`/`fs`/`shell` or a previously-denied area | Tighten the change, or accept it deliberately with `--allow-loosening` |
| `error: cannot read verb registry …` (validate) | The `--registry` file is missing, malformed, or names a verb it does not know | Fix the registry — it is authority, so it fails loud (exit 2) |
| `credential-orphan` (scan) | Credentials present but no manifest on file | Write `manifests/<client>.manifest.yaml` declaring the tool-servers |
| `type-suspect` (scan) | Tool-server name implies more verbs than inferred | Confirm the server's real capabilities; declare them explicitly |
| `expired-ttl` / `unbounded-grant` (scan) | A manifest on file is expired / has an unbounded grant | Bump `ttl`; cap money with `amount.max`, gate `shell.exec` with `approval` |

## Roadmap

- **v0.3 (shipped)** — Cedar export dialect, `manifesto diff` with the
  loosening gate, and the org verb-registry override.
- **v0.4** — attestation & signed receipts (`audit.receipt_of` backed by
  verifiable receipts); manifest signing; richer interop targets (AgentBound,
  Permit.io); JSON Schema emission for editors.
- **v0.5** — drift detection between a live `scan` draft and a committed
  contract, reusing the diff engine.

## License

MIT — see [LICENSE](LICENSE).
