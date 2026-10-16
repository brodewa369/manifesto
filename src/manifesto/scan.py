"""Discovery scanner — read-only, fixture-testable.

``manifesto scan`` looks at the MCP client configs and system signals on a
machine and produces **DRAFT** manifests with provenance annotations, plus a
finding when tool-servers / credentials are present but no manifest is on file.

It is strictly read-only: it only *reads* config files, never writes or spawns.
Tests point it at a synthetic ``fixtures/home`` directory so the real
``~/.hermes`` etc. are never touched.

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

from .schema import load_yaml

# client -> (relative path, servers key)
CLIENT_CONFIGS = {
    "hermes":  (".hermes/config.yaml", "mcp_servers"),
    "claude":  (".claude/claude_desktop_config.json", "mcpServers"),
    "cursor":  (".cursor/mcp.json", "mcpServers"),
    "vscode":  (".config/Code/User/mcp.json", "servers"),
}

#: env-var name fragment that signals a credential is present
CRED_RE = re.compile(r"KEY|TOKEN|SECRET|PASS|CRED|AUTH", re.IGNORECASE)

#: server-name fragment -> (system, [verbs]) conservative inference table
_SERVER_HINTS = [
    ("filesystem", "fs", ["read", "write"]),
    ("file", "fs", ["read", "write"]),
    ("github", "github", ["read", "write"]),
    ("git", "shell", ["exec"]),
    ("shell", "shell", ["exec"]),
    ("terminal", "shell", ["exec"]),
    ("bash", "shell", ["exec"]),
    ("gmail", "gmail", ["read", "send"]),
    ("email", "gmail", ["read", "send"]),
    ("mail", "gmail", ["read", "send"]),
    ("calendar", "calendar", ["read", "create"]),
    ("slack", "slack", ["read", "send"]),
    ("notion", "notion", ["read", "write"]),
    ("browser", "browser", ["navigate", "read"]),
    ("puppeteer", "browser", ["navigate", "read"]),
    ("playwright", "browser", ["navigate", "read"]),
    ("payments", "payments", ["charge"]),
    ("stripe", "payments", ["charge"]),
    ("vault", "fs", ["read"]),
    ("obsidian", "fs", ["read"]),
    ("context7", "http", ["get"]),
    ("docs", "http", ["get"]),
]


def _sanitize_system(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "unknown"


def infer_grants(server_name: str):
    """Return a list of ``(grant_key, note)`` inferred from a server name."""
    low = server_name.lower()
    for frag, system, verbs in _SERVER_HINTS:
        if frag in low:
            return [(f"{system}.{v}", f"inferred from server '{server_name}' (~'{frag}')")
                    for v in verbs]
    # unknown server: conservative read-only guess on a sanitized namespace
    sys = _sanitize_system(server_name)
    return [(f"{sys}.read", f"conservative read-only guess for unknown server '{server_name}'")]


def _read_servers(home: Path, client: str):
    rel, key = CLIENT_CONFIGS[client]
    path = home / rel
    if not path.is_file():
        return None, None
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else load_yaml(text)
    except Exception:
        return path, None
    if not isinstance(data, dict):
        return path, None
    servers = data.get(key)
    if not isinstance(servers, dict):
        return path, None
    return path, servers


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


def scan(home=None, client=None, allow_subprocess=False):
    """Scan a home dir for MCP tool-servers. Returns a dict report.

    ``home`` — a directory to treat as the user's home (fixture dir in tests).
    ``client`` — restrict to one client, or ``None`` for all known clients.
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
        path, servers = _read_servers(home, cl)
        entry = {"client": cl, "path": str(path) if path else None,
                 "present": path is not None,
                 "servers": [], "credentials": [], "draft": None,
                 "provenance": {}, "notes": []}
        if servers:
            entry["servers"] = sorted(servers)
            creds = _collect_credentials(servers)
            entry["credentials"] = [{"server": s, "env": v} for s, v in creds]

            grants = {}
            for sname in sorted(servers):
                for gkey, note in infer_grants(sname):
                    grants.setdefault(gkey, True)
                    entry["provenance"][gkey] = str(path)
                    entry["notes"].append(f"{gkey}: {note}")
            entry["draft"] = {
                "agent": f"{cl}-agent",
                "owner": (report["git_user"] or {}).get("name") or "UNKNOWN",
                "description": f"DRAFT contract auto-inferred from {cl} tool-servers "
                               "(review before enforcing)",
                "grants": grants,
                "deny": [],
                "_provenance": dict(entry["provenance"]),
            }

        manifest_path = _manifest_on_file(home, cl)
        entry["manifest_on_file"] = str(manifest_path) if manifest_path else None
        if (entry["servers"] or entry["credentials"]) and not manifest_path:
            report["findings"].append({
                "code": "no_manifest", "severity": "warning", "client": cl,
                "message": f"{cl}: credentials/tool-servers present but no manifest on file",
                "hint": f"run `manifesto scan --client {cl}` and save the draft as "
                        f"manifests/{cl}.manifest.yaml",
            })
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
        if entry["servers"]:
            out.append(f"   servers: {', '.join(entry['servers'])}")
            for c in entry["credentials"]:
                out.append(f"   credential env: {c['server']}.{c['env']}")
            out.append(f"   draft grants: {', '.join(sorted(entry['draft']['grants']))}")
            prov = entry.get("provenance", {})
            if prov:
                out.append(f"   provenance: {len(prov)} grant(s) traced to {entry['path']}")
            if entry["manifest_on_file"]:
                out.append(f"   manifest on file: {entry['manifest_on_file']}")
        else:
            out.append("   (no tool-servers declared)")
    if report["findings"]:
        out.append("== findings")
        for f in report["findings"]:
            out.append(f"   [{f['severity']}] {f['message']}")
            if f.get("hint"):
                out.append(f"          hint: {f['hint']}")
    else:
        out.append("== no findings")
    return "\n".join(out)
