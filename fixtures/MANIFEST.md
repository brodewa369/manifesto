# fixtures/

Synthetic test data for manifesto. **Nothing here is a real machine** — the
scanner tests point at `fixtures/home/` so the real `~/.hermes`, `~/.claude`,
etc. are never read. Values (tokens, keys) are redacted placeholders.

## Layout

| Path | Purpose |
|------|---------|
| `home/` | A fake home directory scanned by `manifesto scan --home fixtures/home`. Contains `.hermes/config.yaml`, `.claude/claude_desktop_config.json`, `.cursor/mcp.json`, `.config/Code/User/mcp.json`, `.gitconfig`, `crontab.txt`. |
| `home/manifests/hermes.manifest.yaml` | A manifest **on file** for the hermes client — deliberately carries an **expired ttl** and an **unbounded grant** so the scanner's `expired-ttl` and `unbounded-grant` findings are exercised. |
| `unknown-home/` | A second fake home with a single **unrecognized** tool-server, used to prove the scanner is **fail-closed**: unknown servers produce `unknown: true`, never a guessed grant. |
| `diff/` | Paired contracts for `manifesto diff` (see below). |
| `registry/` | An org verb-registry (`acme.yaml`) plus a contract written in that org's vocabulary (`acme-deploy-bot.yaml`) for `validate --registry`. |
| `drift/` | A contract deliberately out of sync with `home/` for `manifesto test` (see below). |
| `fleet/` | Three contracts for `validate --dir`, two of which collide on an agent name (see below). |
| `golden/` | Byte-exact expected output for each export dialect (see below). |

## golden/ — export determinism

`golden/<example>.<target>.expected` holds the exact bytes `manifesto export`
must produce for `examples/<example>.yaml` against `--target <target>`. The
suite (`tests/test_export_golden.py`) compares the live exporter output to
these files byte-for-byte, so any change to an emitter surfaces as a reviewable
diff. Regenerate deliberately with `MANIFESTO_REGOLDEN=1`, then review.

## diff/ — paired contracts for `manifesto diff`

`diff/before.yaml` is a baseline contract; `diff/after-tightened.yaml` and
`diff/after-loosened.yaml` are two candidate revisions of it. They exercise the
classifier and the `--strict` loosening gate from both sides (tightening passes,
loosening exits 1) and back the README demo. All three also validate clean, so
a diff is never run over a broken input.

## drift/ — an out-of-sync contract for `manifesto test`

`drift/agent.manifest.yaml` is written against `home/` but deliberately left
**out of sync**, so the fleet doctor's two bug-classes are exercised at once:
it declares `calendar.read` and `gmail.send` (no such tool-server exists on the
fake home → **PHANTOM** grants) while omitting the `payments`/`shell`/`slack`/
`http` servers that *are* present (→ **UNDECLARED** tools, two of them in a
sensitive class, with `<file>:<line>` provenance). It backs the README's
`manifesto test` demo and `tests/test_doctor.py`. Like every fixture, nothing
here is a real machine.

## fleet/ — multi-agent directory for `validate --dir`

Three contracts for the `--dir` demo: `billing-bot.yaml` and `ops-bot.yaml`
both declare `agent: billing-bot` — a deliberate **agent-name collision** —
while `support-bot.yaml` declares a unique name as the clean contrast. All
three validate individually, so the only failure the batch reports is the
collision, which is exactly the cross-file bug a single-file lint cannot see.
Used by the README's `validate --dir` demo and `tests/test_batch.py`.

## registry/ — org verb-registry override

`registry/acme.yaml` is a synthetic org registry (label `acme-corp`,
`mode: extend`, `unknown_verb: warning`, an `acme` system, and risk classes
with `require_approval: [critical]`). `registry/acme-deploy-bot.yaml` is a
contract written in that vocabulary — it cannot validate until it is checked
against the registry, which is exactly what `validate --registry` is for.
Neither is a real organisation.

### APPROXIMATION — read this before trusting a golden file

Every golden file, and every live export, is an **APPROXIMATION**, not a
certified vendor schema:

- **agentward** — an *AgentWard-style policy YAML skeleton*. AgentWard is a
  stand-in enforcer; the field names (`apiVersion`, `kind`, `spec.rules`,
  `effect: Allow/Deny`) follow the general Kubernetes-ish policy shape that
  many enforcers use, but they are **not** AgentWard's published, versioned
  schema. Justification for freezing these bytes: they pin manifesto's
  *translation* (how a portable contract maps onto a policy skeleton) so the
  translation itself is deterministic and diff-able — not that a specific
  vendor will accept them verbatim.
- **denylist** — a *platform-neutral* deny-list JSON (`defaultEffect: deny`,
  `denyAllExceptGranted: true`, sorted keys). It is intentionally generic: any
  enforcer that can consume a sorted list of actions plus a default-deny flag
  can adapt it. Justification: it pins the canonical, deterministic deny-list
  projection with no vendor coupling at all.
- **cedar** — a *Cedar policy set* (`permit`/`forbid` statements + mapping
  comments). The `//` header carries the manifest→Cedar mapping table with each
  row marked `[APPROXIMATION]`, and the emitted statements keep action ids
  verbatim (`Action::"<system>.<verb>"`). Bounds become `when { context.* }`
  predicates, which Cedar does not itself enforce — the enforcer populates
  `context`. Justification: it pins manifesto's *translation* into a widely
  readable policy language, byte-stable so a Cedar port can be diffed against
  its source contract. **Not vendor-certified and not validated against any
  Cedar engine's schema** — the same warning is in the emitted header and in
  SPEC.md §5.1.

manifesto **never** calls a vendor API and **never** enforces at runtime. Read
the emitted header/disclaimer, reconcile with your enforcer's real schema, then
enforce. The goldens lock determinism and translation stability — they do not
assert vendor conformance.
