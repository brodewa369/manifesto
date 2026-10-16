"""Discovery scanner — read-only, **fail-closed**, fixture-testable.

``manifesto scan`` looks at the MCP client configs and system signals on a
machine and produces **DRAFT** manifests with provenance annotations, plus
findings when tool-servers / credentials / manifests look wrong.

Fail-closed contract (v0.2):

* Inference happens **only** on an *exact* match against the explicit server
  table (:data:`SERVER_TABLE`). There is **no** substring matching and **no**
  "conservative read-only guess" fallback — an unrecognized tool-server or an
  unparseable config yields ``unknown: true``, never a guessed grant.
* Every inferred grant is annotated with **why** it was inferred and **where**
  it came from, as ``<file>:<line>``.

New finding types surfaced here (in addition to ``no_manifest``):

``type-suspect``       tool-server name suggests more verbs than inference allows
``credential-orphan``  credentials present but no manifest on file
``expired-ttl``        a manifest on file carries an expired ttl
``unbounded-grant``    a manifest on file carries an unbounded (money/shell) grant

It is strictly read-only: it only *reads* config files, never writes or spawns.
Tests point it at synthetic fixture homes so real ``~/.hermes`` etc. are never
touched.

Default config locations (relative to the scanned home):

===============  =====================================  ======  ==============
Client           Path                                   Format  Servers key
===============  =====================================  ======  ==============
hermes           ``.hermes/config.yaml``                YAML    ``mcp_servers``
claude           ``.claude/claude_desktop_config.json`` JSON    ``mcpServers``
cursor           ``.cursor/mcp.json``                   JSON    ``mcpServers``
vscode           ``.config/Code/User/mcp.json``         JSON    ``servers``
===============  =====================================  ======  ==============
"""
from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

from .schema import (ERROR, KNOWN, bound_requirement, is_money_grant, load_yaml,
                     split_findings, validate)

# client -> (relative path, servers key)
CLIENT_CONFIGS = {
    "hermes":  (".hermes/config.yaml", "mcp_servers"),
    "claude":  (".claude/claude_desktop_config.json", "mcpServers"),
    "cursor":  (".cursor/mcp.json", "mcpServers"),
    "vscode":  (".config/Code/User/mcp.json", "servers"),
}

#: env-var name fragment that signals a credential is present
CRED_RE = re.compile(r"KEY|TOKEN|SECRET|PASS|CRED|AUTH", re.IGNORECASE)

#: EXACT server-name -> (system, [verbs]) inference table. A tool-server is
#: inferred **only** on an exact (case-insensitive) match of its full name.
#: Anything not listed here is fail-closed to ``unknown: true``.
SERVER_TABLE = {
    "filesystem":     ("fs", ["read", "write"]),
    "file":           ("fs", ["read", "write"]),
    "github":         ("github", ["read", "write"]),
    "github-mcp":     ("github", ["read", "write"]),
    "git":            ("shell", ["exec"]),
    "shell":          ("shell", ["exec"]),
    "broken-shell":   ("shell", ["exec"]),
    "terminal":       ("shell", ["exec"]),
    "bash":           ("shell", ["exec"]),
    "gmail":          ("gmail", ["read", "send"]),
    "email":          ("gmail", ["read", "send"]),
    "mail":           ("gmail", ["read", "send"]),
    "calendar":       ("calendar", ["read", "create"]),
    "slack":          ("slack", ["read", "send"]),
    "notion":         ("notion", ["read", "write"]),
    "browser":        ("browser", ["navigate", "read"]),
    "puppeteer":      ("browser", ["navigate", "read"]),
    "playwright":     ("browser", ["navigate", "read"]),
    "payments":       ("payments", ["charge"]),
    "stripe":         ("payments", ["charge"]),
    "vault":          ("fs", ["read"]),
    "obsidian":       ("fs", ["read"]),
    "semantic-vault": ("fs", ["read"]),
    "context7":       ("http", ["get"]),
    "docs":           ("http", ["get"]),
}

#: A capability word embedded in a server NAME that implies a verb. If the word
#: implies a verb the exact-match inference did NOT grant, the name is
#: *type-suspect*: it suggests more power than we inferred (or, for unknown
#: servers, more power than the fail-closed ``unknown`` marker represents).
_CAPABILITY_TOKENS = {
    "delete": "delete", "erase": "delete", "remove": "delete", "drop": "delete",
    "send": "send", "mail": "send", "post": "send", "push": "send",
    "write": "write", "edit": "write", "create": "write", "update": "write",
    "charge": "charge", "pay": "charge", "payout": "payout", "refund": "refund",
    "exec": "exec", "run": "exec", "shell": "exec", "bash": "exec",
    "merge": "merge",
    "admin": "*", "root": "*", "full": "*", "superuser": "*",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _sanitize_system(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "unknown"


def infer_grants(server_name: str, source: str | None = None):
    """Fail-closed grant inference for one tool-server.

    Returns a decision dict::

        {
          "known":   bool          # exact table match
          "system":  str | None    # inferred system (known servers only)
          "grants":  {key: True}   # inferred grants (empty when unknown)
          "notes":   [str]         # why, annotated with <file>:<line>
        }

    Inference happens **only** on an exact match of ``server_name`` against
    :data:`SERVER_TABLE`. An unrecognized server is recorded as ``unknown``
    (``grants`` stays empty) — never a guessed capability.
    """
    key = (server_name or "").strip().lower()
    why = f"{source}: " if source else ""

    if key in SERVER_TABLE:
        system, verbs = SERVER_TABLE[key]
        grants = {f"{system}.{v}": True for v in verbs}
        notes = [f"{why}exact match '{key}' -> {system} verbs [{', '.join(verbs)}]"]
        suspect = _type_suspect(key, set(verbs))
        return {"known": True, "system": system, "grants": grants,
                "notes": notes, "type_suspect": suspect}

    # fail-closed: unrecognized server -> unknown, no guessed grant
    suspect = _type_suspect(key, set())
    notes = [f"{why}unrecognized tool-server '{server_name}' — fail-closed: "
             "no grant inferred (unknown: true)"]
    return {"known": False, "system": None, "grants": {},
            "notes": notes, "type_suspect": suspect}


def _type_suspect(name: str, inferred_verbs: set):
    """Return a reason string if the NAME implies verbs we did not infer."""
    for token in _TOKEN_RE.findall(name.lower()):
        implied = _CAPABILITY_TOKENS.get(token)
        if implied is None:
            continue
        if implied == "*":
            return f"server name '{name}' reads as privileged ('{token}') but " \
                   "inference is bounded"
        if implied not in inferred_verbs:
            return (f"server name '{name}' suggests verb '{implied}' "
                    f"(via '{token}') beyond inferred [{', '.join(sorted(inferred_verbs)) or 'none'}]")
    return None


def _find_server_line(text: str, name: str):
    """Return the 1-based line number where ``name`` is declared as a key."""
    pat = re.compile(r'^\s*"?' + re.escape(name) + r'"?\s*:', re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1


def _read_servers(home: Path, client: str):
    """Return ``(path, servers, raw_text, error)``.

    ``error`` is ``None`` on success, ``"missing"`` when the file is absent,
    ``"unreadable"`` on I/O failure, ``"unparsable"`` on a parse failure, and
    ``"wrong-shape"`` when the servers key is not a mapping.
    """
    rel, key = CLIENT_CONFIGS[client]
    path = home / rel
    if not path.is_file():
        return None, None, None, "missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path, None, None, "unreadable"
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else load_yaml(text)
    except Exception:
        return path, None, text, "unparsable"
    if not isinstance(data, dict):
        return path, None, text, "wrong-shape"
    servers = data.get(key)
    if not isinstance(servers, dict):
        return path, None, text, "wrong-shape"
    return path, servers, text, None


def _collect_credentials(servers: dict):
    creds = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        env = cfg.get("env")
        if isinstance(env, dict):
            for var in env:
                if CRED_RE.search(str(var)):
                    creds.append((name, str(var)))
    return creds


def _read_git_user(home: Path):
    for candidate in (home / ".gitconfig", home / ".config/git/config"):
        if candidate.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(candidate)
                name = cp.get("user", "name", fallback=None)
                email = cp.get("user", "email", fallback=None)
                if name or email:
                    return {"name": name, "email": email, "source": str(candidate)}
            except Exception:
                pass
    return None


def _read_crontab(home: Path, allow_subprocess: bool):
    """Fixture-safe crontab: read ``crontab.txt`` under home if present.

    Only shells out to ``crontab -l`` when scanning a real home and no fixture
    file exists — tests always pass a fixture home, so the real crontab is
    never touched in tests.
    """
    fixture = home / "crontab.txt"
    if fixture.is_file():
        lines = [ln for ln in fixture.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        return {"lines": lines, "source": str(fixture)}
    if allow_subprocess:
        import subprocess
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                lines = [ln for ln in out.stdout.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]
                return {"lines": lines, "source": "crontab -l"}
        except Exception:
            pass
    return None


def _manifest_on_file(home: Path, client: str):
    """A manifest is 'on file' if a per-client or generic one exists."""
    candidates = [
        home / "manifests" / f"{client}.manifest.yaml",
        home / "agent.manifest.yaml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _check_manifest_health(manifest_path: Path, now=None):
    """Load a manifest on file and surface expired-ttl / unbounded-grant.

    Returns a list of finding dicts. Fail-closed: an unreadable manifest is
    itself reported rather than ignored.
    """
    from datetime import date
    from .schema import ManifestError, _parse_iso_date, load_manifest_file

    now = now or date.today()
    out = []
    try:
        manifest = load_manifest_file(manifest_path)
    except ManifestError as exc:
        out.append({"code": "unreadable_manifest", "severity": "error",
                    "message": f"manifest on file {manifest_path} is unreadable: {exc}",
                    "hint": "fix or remove the manifest so it can be checked"})
        return out

    # expired ttl
    ttl_raw = manifest.get("ttl")
    ttl_date = _parse_iso_date(ttl_raw) if isinstance(ttl_raw, str) else None
    if ttl_date is not None and ttl_date < now:
        out.append({"code": "expired-ttl", "severity": "warning",
                    "message": f"{manifest_path.name}: ttl {ttl_date.isoformat()} is in the past",
                    "hint": "bump `ttl` or retire this agent"})

    # unbounded grants (money / shell without bounds)
    findings = validate(manifest, now=now)
    unbounded = sorted({f.keypath for f in findings
                        if f.code == "unbounded_grant" and f.severity == ERROR})
    if unbounded:
        out.append({"code": "unbounded-grant", "severity": "warning",
                    "message": f"{manifest_path.name}: unbounded grant(s): "
                               + ", ".join(k.replace("grants.", "") for k in unbounded),
                    "hint": "cap money verbs with amount.max; gate shell.exec with approval"})
    return out


def scan(home=None, client=None, allow_subprocess=False, now=None):
    """Scan a home dir for MCP tool-servers. Returns a dict report.

    ``home`` — a directory to treat as the user's home (fixture dir in tests).
    ``client`` — restrict to one client, or ``None`` for all known clients.
    ``now`` — override the current date (for fixture-expired manifests).
    """
    home = Path(home) if home else Path.home()
    clients = [client] if client else list(CLIENT_CONFIGS)
    report = {
        "home": str(home),
        "clients": [],
        "git_user": _read_git_user(home),
        "crontab": _read_crontab(home, allow_subprocess=allow_subprocess),
        "findings": [],
    }

    for cl in clients:
        if cl not in CLIENT_CONFIGS:
            report["findings"].append({
                "code": "unknown_client", "severity": "error",
                "message": f"unknown client '{cl}'",
                "hint": f"choose one of: {', '.join(sorted(CLIENT_CONFIGS))}",
            })
            continue
        path, servers, raw_text, error = _read_servers(home, cl)
        entry = {"client": cl, "path": str(path) if path else None,
                 "present": path is not None,
                 "servers": [], "credentials": [], "draft": None,
                 "provenance": {}, "notes": [], "unknowns": [],
                 "parse_error": error if error not in (None, "missing") else None}

        if servers:
            entry["servers"] = sorted(servers)
            creds = _collect_credentials(servers)
            entry["credentials"] = [{"server": s, "env": v} for s, v in creds]

            grants = {}
            for sname in sorted(servers):
                line = _find_server_line(raw_text, sname) if raw_text else None
                src = f"{path}:{line}" if line else str(path)
                decision = infer_grants(sname, source=src)
                for gkey, gval in decision["grants"].items():
                    grants.setdefault(gkey, gval)
                    entry["provenance"][gkey] = src
                entry["notes"].extend(decision["notes"])
                if not decision["known"]:
                    entry["unknowns"].append(sname)
                suspect = decision.get("type_suspect")
                if suspect:
                    report["findings"].append({
                        "code": "type-suspect", "severity": "warning",
                        "client": cl, "server": sname,
                        "message": f"{cl}/{sname}: {suspect}",
                        "hint": "confirm the tool-server's real capabilities, "
                                "then declare them explicitly in a manifest",
                    })
            if entry["unknowns"]:
                grants["unknown"] = True  # fail-closed sentinel, never a guess
            entry["draft"] = {
                "agent": f"{cl}-agent",
                "owner": (report["git_user"] or {}).get("name") or "UNKNOWN",
                "description": f"DRAFT contract auto-inferred from {cl} tool-servers "
                               "(review before enforcing)",
                "grants": grants,
                "deny": [],
                "_provenance": dict(entry["provenance"]),
            }
        elif error in ("unparsable", "wrong-shape", "unreadable"):
            # fail-closed: a config we cannot read must not be treated as empty
            entry["draft"] = {
                "agent": f"{cl}-agent",
                "owner": (report["git_user"] or {}).get("name") or "UNKNOWN",
                "description": f"DRAFT for {cl}: config could not be parsed "
                               f"({error}) — nothing inferred (unknown: true)",
                "grants": {"unknown": True},
                "deny": [],
                "_provenance": {},
            }
            report["findings"].append({
                "code": "unparsable-config", "severity": "error", "client": cl,
                "message": f"{cl}: config {path} could not be parsed ({error})",
                "hint": "fix the config; nothing was inferred (fail-closed)",
            })

        manifest_path = _manifest_on_file(home, cl)
        entry["manifest_on_file"] = str(manifest_path) if manifest_path else None

        if (entry["servers"] or entry["credentials"]) and not manifest_path:
            report["findings"].append({
                "code": "no_manifest", "severity": "warning", "client": cl,
                "message": f"{cl}: credentials/tool-servers present but no manifest on file",
                "hint": f"run `manifesto scan --client {cl}` and save the draft as "
                        f"manifests/{cl}.manifest.yaml",
            })
            if entry["credentials"]:
                report["findings"].append({
                    "code": "credential-orphan", "severity": "warning",
                    "client": cl,
                    "message": f"{cl}: {len(entry['credentials'])} credential(s) "
                               "with no manifest on file",
                    "hint": f"write manifests/{cl}.manifest.yaml declaring which "
                            "tool-servers may use these credentials",
                })

        if manifest_path:
            report["findings"].extend(_check_manifest_health(manifest_path, now=now))

        report["clients"].append(entry)

    return report


def render(report) -> str:
    """Human-readable scan report."""
    out = [f"== manifesto scan | home={report['home']}"]
    gu = report.get("git_user")
    if gu:
        out.append(f"   git user: {gu.get('name')} <{gu.get('email')}>")
    ct = report.get("crontab")
    if ct:
        out.append(f"   crontab: {len(ct['lines'])} job(s) from {ct['source']}")
    for entry in report["clients"]:
        status = "present" if entry["present"] else "absent"
        out.append(f"-- {entry['client']: <8} {status: <8} {entry['path'] or ''}")
        if entry.get("parse_error"):
            out.append(f"   parse error: {entry['parse_error']} (fail-closed, nothing inferred)")
        if entry["servers"]:
            out.append(f"   servers: {', '.join(entry['servers'])}")
            for c in entry["credentials"]:
                out.append(f"   credential env: {c['server']}.{c['env']}")
            grants = (entry.get("draft") or {}).get("grants", {})
            out.append(f"   draft grants: {', '.join(sorted(grants))}")
            if entry.get("unknowns"):
                out.append(f"   unknown server(s): {', '.join(sorted(entry['unknowns']))} "
                           "-> unknown: true (no grant guessed)")
            prov = entry.get("provenance", {})
            if prov:
                sample = next(iter(prov.values()))
                out.append(f"   provenance: {len(prov)} grant(s), e.g. {sample}")
            if entry["manifest_on_file"]:
                out.append(f"   manifest on file: {entry['manifest_on_file']}")
        elif entry.get("draft"):
            out.append(f"   draft grants: {', '.join(sorted(entry['draft']['grants']))}")
        else:
            out.append("   (no tool-servers declared)")
    if report["findings"]:
        out.append("== findings")
        for f in report["findings"]:
            out.append(f"   [{f['severity']}] {f['code']}: {f['message']}")
            if f.get("hint"):
                out.append(f"          hint: {f['hint']}")
    else:
        out.append("== no findings")
    return "\n".join(out)
