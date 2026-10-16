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
| `golden/` | Byte-exact expected output for each export dialect (see below). |

## golden/ — export determinism

`golden/<example>.<target>.expected` holds the exact bytes `manifesto export`
must produce for `examples/<example>.yaml` against `--target <target>`. The
suite (`tests/test_export_golden.py`) compares the live exporter output to
these files byte-for-byte, so any change to an emitter surfaces as a reviewable
diff. Regenerate deliberately with `MANIFESTO_REGOLDEN=1`, then review.

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

manifesto **never** calls a vendor API and **never** enforces at runtime. Read
the emitted header/disclaimer, reconcile with your enforcer's real schema, then
enforce. The goldens lock determinism and translation stability — they do not
assert vendor conformance.
