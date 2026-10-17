# Changelog

All notable changes to manifesto. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

## [0.3.0] — 2026-10-17

Enrichment release: a third interop dialect, contract review with a loosening
gate, and an org-level verb registry.

### Added
- **Cedar export dialect** — `manifesto export --target cedar` emits a Cedar
  policy set: `permit` per grant, `forbid` per `deny` entry (a Cedar `forbid`
  always wins, matching the existing "deny dominates" rule), an extra
  `permit` per delegated child grant, and mapping/`ttl`/`audit` as `//`
  comments. Action ids are preserved verbatim; `amount`/`rate`/`to_domains`/
  `approval` become `when { context.* }` predicates the enforcer supplies.
  The manifest→Cedar mapping table with per-row fidelity marks (verbatim /
  exact / approximation) is in `SPEC.md` §5.1, headed by the honest caveat
  that these overrides are **approximations validated byte-stable and
  reviewed manually — not vendor-certified**. Goldens:
  `fixtures/golden/<example>.cedar.expected` for every example, byte-compared
  by the same `MANIFESTO_REGOLDEN=1` mechanism as the other dialects.
- **`manifesto diff <old> <new>`** — reports grants **added / removed /
  tightened / loosened**, `deny` entries added/removed, expiry changes,
  delegation changes and `audit.receipt_of` changes as a human table or
  `--json`. Ties (identical caps/domains/gates/delegations) produce no rows;
  a grant that moves both ways resolves worst-case (**loosened**). The
  **loosening gate**: a loosening touching a sensitive class (`payments`,
  `fs`/`files`, `shell`) or a previously explicitly-denied key/namespace
  exits **1** under `--strict`, or **0** with `--allow-loosening`. Loosening
  elsewhere is reported but does not fail the build. Exit codes 0/1/2; output
  is deterministic. Fixtures: `fixtures/diff/{before,after-tightened,after-loosened}.yaml`.
- **Org verb-registry override** — `manifesto validate --registry FILE` layers
  an org file over the built-in `KNOWN` registry: `mode: extend|replace`,
  `unknown_verb: error|warning|ignore` (unknown verbs are marked per that
  rule), `systems:` for org-owned verbs, and `risk_classes:` +
  `require_approval:` so a gated class granted without `approval: required`
  raises **`missing_approval`**. The registry is authority: it is parsed
  strictly (bad `mode`, unknown top-level key, or a risk class naming an
  unknown verb → `ManifestError`, CLI exit `2`), and `validate --json` carries
  a `registry` block. Fixtures: `fixtures/registry/{acme.yaml,acme-deploy-bot.yaml}`.

### Changed
- Exporters carry `manifesto 0.3.0` in their header/generator line; all
  twelve golden files regenerated (`MANIFESTO_REGOLDEN=1`) and reviewed.
- `TARGETS` now lists `agentward`, `denylist`, `cedar`; `validate` gained
  `--registry`, and the CLI gained the `diff` subcommand.
- Docs: SPEC gains §5.1 (Cedar mapping), §6 (diff + gate), §7 (registry) and
  the Changelog moves to §8; README gains recorded end-to-end output for the
  `cedar`, `diff` and `registry` flows.

### Notes
- 160 tests, all passing (was 87 at 0.2.0: +44 diff, +28 registry, +1 cedar
  golden coverage; no test removed). Zero runtime dependencies (pure stdlib).

## [0.2.0] — 2026-10-17

Hardening release: formal schema consumption, a fail-closed scanner, delegation
attenuation, and release hygiene.

### Added
- **Published machine-readable schema** — `schema/agent-manifest.schema.json`
  (JSON Schema draft-07 subset: `type`, `required`, `enum`, `pattern`,
  `propertyNames`, `additionalProperties`, local `$ref`).
- **Schema-derived structural validation** — `manifesto/jsonschema_mini.py`, a
  tiny draft-07-subset engine that walks the published schema at call time.
  Structural findings (types, required, enums, grant-key pattern, unknown keys)
  come from the *file*, not from duplicated code. No `jsonschema` dependency.
- **Canary test** (`tests/test_schema_canary.py`) — mutates the schema (makes
  `ttl` required, drops `agent` from required, widens the approval enum, relaxes
  the key pattern) and proves validator behaviour follows the file.
- **Golden-file export suite** — `fixtures/golden/*.expected` byte-compares both
  dialects (`agentward`, `denylist`) across all examples for determinism;
  `fixtures/MANIFEST.md` labels them APPROXIMATION with justification.
  Regenerate deliberately with `MANIFESTO_REGOLDEN=1`.
- **Delegation attenuation** (`delegations:`) — RFC-0001 §3.3. A child's
  `narrower_than` grant set must be a narrowed subset of the parent's (`from`),
  `purpose` required. Escalation rejected, including taking a verb the parent
  holds only via a constraint as a bare `true`. `explain` renders attenuation
  chains; exporters carry delegations deterministically. Inspired by
  Macaroon/Biscuit caveat semantics.
- `examples/delegating-lead.yaml` — a valid attenuation example.

### Changed
- **Scanner is fail-closed** — grant inference now happens *only* on an exact
  match against an explicit `SERVER_TABLE` (no substring matching, no
  "conservative read-only guess"). An unrecognized tool-server or an unparseable
  config yields `unknown: true`, never a guessed capability.
- **Scanner provenance** — every inferred grant is annotated `<file>:<line>`.
- **Scanner findings** — added `type-suspect` (name suggests more verbs than
  inferred), `credential-orphan` (credentials present, no manifest on file),
  `expired-ttl`, and `unbounded-grant` (surfaced from a manifest on file).
- Validator refactored into a structural (schema-derived) layer plus a semantic
  (registry/business-rules) layer; behaviour of every existing finding class is
  preserved.

### Notes
- 87 tests, all passing. Zero runtime dependencies (pure stdlib).

## [0.1.0] — 2026-10-17

Initial release.

### Added
- Manifest schema: `agent`, `version`, `owner`, `description`, `ttl`, `grants`,
  `deny`, `audit`, `meta`.
- `<system>.<verb>` namespace with a known-systems registry and "did you mean"
  suggestions.
- Bounded-required enforcement for money verbs (`amount.max`) and `shell.exec`
  (`approval: required`).
- Hand-rolled validator (no `jsonschema`): unknown keys, unknown/ungroupable
  verbs, unbounded grants, empty grants, shadowed grants, missing/expired ttl,
  audit-of-non-granted-key — each with key-path, message, and fix hint.
- Read-only discovery scanner for hermes/claude/cursor/vscode + crontab + git
  user, with provenance and a "no manifest on file" finding.
- Deterministic exporters (AgentWard-style policy YAML, deny-list JSON).
- Zero runtime dependencies.
