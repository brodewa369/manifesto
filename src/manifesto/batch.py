"""Multi-manifest handling — ``--dir`` for ``validate`` and ``explain``.

A fleet is a *directory* of contracts, not one file. ``manifesto validate
--dir manifests/`` (and ``explain --dir``) loads **every** ``*.yaml`` /
``*.yml`` / ``*.json`` in the directory, validates/renders them together, and
adds the one bug class that is invisible to any single-file lint: a
**cross-file agent-name collision** — two contracts claiming the same
``agent`` name. In a fleet that name is the subject an enforcer matches on, so
a collision means one of the two contracts silently wins; that is an error,
not a warning.

Guarantees:

* **Deterministic** — files are processed in sorted path order; a collision
  names both files; the same directory always yields the same report.
* **Fail-closed** — a file that cannot be parsed is reported as an error, and
  the batch exits ``1`` (a broken contract must never look clean).
* **Exit codes** — ``0`` every manifest valid and no collision; ``1`` any
  error-severity finding (including a collision), or a warning under
  ``--strict``; ``2`` usage error / unreadable directory.
"""
from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

from .schema import (ManifestError, load_manifest_file, split_findings,
                     validate)

#: File suffixes treated as manifests inside a ``--dir``.
MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")

#: One loaded manifest in a batch. ``findings`` is ``None`` when the file
#: could not be parsed (the error is carried in ``load_error``).
BatchEntry = namedtuple("BatchEntry",
                        ["path", "agent", "data", "findings", "load_error"])


def discover(directory) -> list:
    """Sorted manifest paths inside ``directory`` (deterministic)."""
    d = Path(directory)
    if not d.is_dir():
        raise ManifestError(f"not a directory: {d}")
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in MANIFEST_SUFFIXES)


def load_batch(directory) -> list:
    """Load every manifest in ``directory`` -> list of :class:`BatchEntry`.

    A parse failure does not abort the batch; it becomes an entry with
    ``load_error`` set (fail-closed: surfaced, never skipped).
    """
    entries = []
    for path in discover(directory):
        try:
            data = load_manifest_file(path)
        except ManifestError as exc:
            entries.append(BatchEntry(path, None, None, None, str(exc)))
            continue
        agent = data.get("agent") if isinstance(data, dict) else None
        entries.append(BatchEntry(path, agent, data, None, None))
    return entries


def validate_batch(directory, *, now=None, schema_path_=None,
                   registry=None) -> dict:
    """Validate every manifest in ``directory`` and check name collisions.

    Returns a report dict::

        {
          "directory": str,
          "entries": [ {path, agent, valid, error_count, warning_count,
                        findings: [...] } ... ],
          "collisions": [ {agent, files: [...]} ... ],
          "summary": {files, valid, invalid, collisions, errors, warnings},
        }
    """
    entries = load_batch(directory)
    seen = {}
    out_entries = []
    total_errors = total_warnings = 0

    for e in entries:
        if e.load_error is not None:
            out_entries.append({
                "path": str(e.path), "agent": None, "valid": False,
                "error_count": 1, "warning_count": 0, "load_error": e.load_error,
                "findings": [{"keypath": "(root)", "code": "unreadable_manifest",
                              "severity": "error",
                              "message": f"cannot load manifest: {e.load_error}",
                              "hint": "fix or remove the file"}],
            })
            total_errors += 1
            continue

        findings = validate(e.data, now=now, schema_path_=schema_path_,
                            registry=registry)
        errors, warnings = split_findings(findings)
        out_entries.append({
            "path": str(e.path), "agent": e.agent, "valid": not errors,
            "error_count": len(errors), "warning_count": len(warnings),
            "findings": [f._asdict() for f in findings],
        })
        total_errors += len(errors)
        total_warnings += len(warnings)
        if isinstance(e.agent, str) and e.agent:
            seen.setdefault(e.agent, []).append(str(e.path))

    collisions = [{"agent": agent, "files": sorted(files)}
                  for agent, files in sorted(seen.items())
                  if len(files) > 1]

    return {
        "directory": str(directory),
        "entries": out_entries,
        "collisions": collisions,
        "summary": {
            "files": len(entries),
            "valid": sum(1 for x in out_entries if x["valid"]),
            "invalid": sum(1 for x in out_entries if not x["valid"]),
            "collisions": len(collisions),
            "errors": total_errors + len(collisions),
            "warnings": total_warnings,
        },
    }


def render_batch(report) -> str:
    """Human report for a ``--dir`` validation (incl. collisions)."""
    out = [f"== manifesto validate --dir | {report['directory']}"]
    for e in report["entries"]:
        name = e["agent"] or "(no agent)"
        status = "ok" if e["valid"] else "INVALID"
        out.append(f"-- {e['path']}  [{name}]  {status}")
        if e.get("load_error"):
            out.append(f"   ERROR   (root): cannot load manifest: {e['load_error']}")
            continue
        for f in e["findings"]:
            arrow = f["keypath"] or "(root)"
            out.append(f"   {f['severity'].upper(): <7} {arrow}: {f['message']}")
            if f.get("hint"):
                out.append(f"          fix: {f['hint']}")
    if report["collisions"]:
        out.append("== agent-name collisions")
        for c in report["collisions"]:
            out.append(f"   COLLISION  agent '{c['agent']}' claimed by: "
                       + ", ".join(c["files"]))
    s = report["summary"]
    out.append(f"== {s['files']} file(s): {s['valid']} valid, {s['invalid']} invalid; "
               f"{s['collisions']} collision(s); {s['errors']} error(s), "
               f"{s['warnings']} warning(s)")
    return "\n".join(out)


def render_explain_batch(directory) -> str:
    """Render ``manifesto explain`` for every manifest in ``directory``."""
    from .explain import explain as explain_fn
    entries = load_batch(directory)
    blocks = []
    for e in entries:
        if e.load_error is not None:
            blocks.append(f"== {e.path}\n   (unreadable: {e.load_error})")
            continue
        blocks.append(f"== {e.path}\n" + explain_fn(e.data))
    return "\n\n".join(blocks)


def as_json(report) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, default=str)


__all__ = ["MANIFEST_SUFFIXES", "BatchEntry", "discover", "load_batch",
           "validate_batch", "render_batch", "render_explain_batch", "as_json"]
