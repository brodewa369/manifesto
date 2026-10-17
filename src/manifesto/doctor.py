"""``manifesto test`` — the fleet doctor (drift + phantom/undeclared audit).

A self-declared contract only ever contains what its author *remembered*.
``manifesto test`` cross-checks a manifest against **reality** — the same
home ``manifesto scan`` reads — and surfaces the two bug classes a validator
structurally *cannot* see, because they are not shape errors but drift
between the contract and the machine:

* **UNDECLARED** — a tool-server is present on disk (an MCP config names it)
  but its inferred capability is **absent** from the declared manifest. The
  agent can actually do something the contract never accounted for. Reported
  with the ``<file>:<line>`` provenance of the offending server.
* **PHANTOM** — the manifest claims a grant but **no corresponding tool** was
  found on disk. A dead contract entry: authority that names nothing real.

Both are reconciled through the same **fail-closed** inference
(:func:`manifesto.scan.infer_grants`) the scanner uses: a tool is only ever
mapped on an exact server-table match, so an unknown server never becomes a
guessed grant — it stays ``unknown`` and is ignored by the reconciliation
(except to mark the client as having unattributed servers).

Sensitive classes (``payments``, ``fs``/``files``, ``shell``) are shared with
the ``diff`` gate. An **UNDECLARED** tool in a sensitive class is the dangerous
direction (real capability the contract forgot), so ``--strict`` makes it exit
``1``; undeclared tools elsewhere, and phantoms, are reported but do not fail
the build unless ``--strict``.

Output is a human table or ``--json``; deterministically ordered.
"""
from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

from .scan import CLIENT_CONFIGS, infer_grants, scan
from .schema import ManifestError, load_manifest_file

#: Grant-key -> sensitive class (``files`` is the same surface as ``fs``).
SENSITIVE_CLASSES = {"payments": "payments", "fs": "fs", "files": "fs",
                     "shell": "shell"}


def _sensitive_of(key) -> str:
    return SENSITIVE_CLASSES.get(str(key).partition(".")[0], "")


Finding = namedtuple("Finding", ["kind", "key", "detail", "source", "sensitive"])


def _system_of(key: str) -> str:
    return str(key).partition(".")[0]


def _declared_systems(declared_grants) -> set:
    return {_system_of(k) for k in declared_grants}


def test_fleet(manifest, home=None, client=None, now=None,
               allow_subprocess=False) -> dict:
    """Audit ``manifest`` against the tool-servers present under ``home``.

    ``manifest``    — a loaded manifest mapping (the *declared* contract).
    ``home``        — directory treated as the user's home (fixture dir in tests).
    ``client``      — restrict discovery to one client, or ``None`` for all.
    ``now``         — clock override handed to the underlying scan.
    ``allow_subprocess`` — permit ``crontab -l`` when scanning a real home.

    Returns a report dict with ``undeclared`` / ``phantom`` finding lists, a
    ``summary`` block, and the raw scan's per-client servers for context.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a mapping")

    agent = manifest.get("agent", "(unnamed)")
    grants = manifest.get("grants") if isinstance(manifest.get("grants"), dict) else {}
    declared = set(grants)
    declared_sys = _declared_systems(declared)

    report = scan(home=home, client=client, allow_subprocess=allow_subprocess,
                  now=now)
    home_path = report["home"]

    # ---- discover what is actually present, with provenance --------------- #
    # present: {server_name: {"client":..., "source": "<file>:<line>",
    #                         "system":..., "grants": {...}}}
    present = {}
    for entry in report["clients"]:
        cl = entry["client"]
        cfg_path = entry.get("path")
        raw_text = None
        if cfg_path:
            try:
                raw_text = Path(cfg_path).read_text(encoding="utf-8")
            except OSError:
                raw_text = None
        for server in entry.get("servers", []):
            decision = infer_grants(server, source=None)
            source = cfg_path or ""
            if raw_text:
                from .scan import _find_server_line
                line = _find_server_line(raw_text, server)
                if line:
                    source = f"{cfg_path}:{line}"
            present.setdefault(server, {
                "client": cl, "source": source,
                "system": decision.get("system"),
                "known": decision.get("known", False),
                "grants": decision.get("grants", {}),
            })

    # ---- UNDECLARED: on disk, absent from the manifest -------------------- #
    undeclared = []
    for server in sorted(present):
        info = present[server]
        if not info["known"]:
            continue  # fail-closed unknown: never a guessed grant, skip drift
        srv_system = info["system"]
        srv_grant_keys = set(info["grants"])
        # covered when the manifest already grants the same system
        if srv_system in declared_sys:
            continue
        # also covered if any exact inferred key is declared
        if srv_grant_keys & declared:
            continue
        undeclared.append(Finding(
            "UNDECLARED", server,
            f"{info['client']}/{server} -> {srv_system} present on disk but "
            f"'{srv_system}.*' is absent from the declared manifest",
            info["source"], _sensitive_of(f"{srv_system}.x")))

    # ---- PHANTOM: declared in the manifest, no tool found on disk --------- #
    present_systems = {info["system"] for info in present.values()
                       if info.get("system")}
    phantom = []
    for key in sorted(declared):
        system = _system_of(key)
        if system in present_systems:
            continue
        phantom.append(Finding(
            "PHANTOM", key,
            f"manifest grants '{key}' but no '{system}' tool-server was found "
            "under the scanned home (dead contract entry)",
            "", _sensitive_of(key)))

    findings = sorted(undeclared + phantom,
                      key=lambda f: (f.kind, f.key))

    strict_sensitive = [f for f in findings
                        if f.kind == "UNDECLARED" and f.sensitive]

    return {
        "manifest_agent": agent,
        "home": home_path,
        "declared_grants": sorted(declared),
        "present_tools": sorted(present),
        "undeclared": [f._asdict() for f in findings if f.kind == "UNDECLARED"],
        "phantom": [f._asdict() for f in findings if f.kind == "PHANTOM"],
        "findings": [f._asdict() for f in findings],
        "summary": {
            "declared": len(declared),
            "present_tools": len(present),
            "undeclared": len(undeclared),
            "phantom": len(phantom),
            "strict_sensitive": len(strict_sensitive),
            "total": len(findings),
        },
    }


def render(report) -> str:
    """Human table for a fleet-doctor report."""
    out = [f"== manifesto test | agent={report['manifest_agent']} | home={report['home']}"]
    out.append(f"   declared grants: {report['summary']['declared']} | "
               f"tools on disk: {report['summary']['present_tools']}")
    findings = report["findings"]
    if not findings:
        out.append("   no drift: every tool is declared, every grant has a tool")
    else:
        w_key = max([len("KIND")] + [len(f["kind"]) for f in findings] +
                    [len(f["key"]) for f in findings])
        out.append(f"   {'KIND':<9} {'KEY':<{w_key}}  DETAIL")
        for f in findings:
            src = f"  [{f['source']}]" if f.get("source") else ""
            out.append(f"   {f['kind']:<9} {f['key']:<{w_key}}  {f['detail']}{src}")
    s = report["summary"]
    out.append(f"== {s['undeclared']} undeclared tool(s), {s['phantom']} phantom "
               f"grant(s); {s['total']} finding(s) total")
    if s["strict_sensitive"]:
        out.append(f"!! --strict: {s['strict_sensitive']} UNDECLARED tool(s) in a "
                   "sensitive class (payments/fs/shell) -> exit 1")
    return "\n".join(out)


def as_json(report) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)


__all__ = ["test_fleet", "render", "as_json", "Finding", "SENSITIVE_CLASSES"]
