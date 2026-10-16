# Changelog

All notable changes to manifesto. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows [SemVer](https://semver.org/).

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
