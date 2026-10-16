"""Plain-language explanations of a manifest, with risk badges.

``manifesto explain`` turns a contract into a sentence a human can read:
what the agent MAY do, what it may NOT do, and which risk badges apply
(``unbounded``, ``expired``, ``dominated``).
"""
from __future__ import annotations

from datetime import date

from .schema import (APPROVAL_GATED, KNOWN, _parse_iso_date, bound_requirement,
                     is_money_grant, split_findings, validate)

#: short human phrase per well-known verb; falls back to the key itself.
_PHRASES = {
    "gmail.read": "read mail",
    "gmail.send": "send email",
    "gmail.draft": "draft email",
    "gmail.delete": "delete mail",
    "calendar.read": "read the calendar",
    "calendar.create": "create events",
    "payments.charge": "charge money",
    "payments.refund": "refund money",
    "fs.read": "read files",
    "fs.write": "write files",
    "fs.delete": "delete files",
    "shell.exec": "run shell commands",
    "http.get": "make outbound GET requests",
    "github.read": "read GitHub",
    "github.write": "write to GitHub",
    "browser.navigate": "drive a browser",
    "slack.send": "send Slack messages",
}


def describe(key: str) -> str:
    """Human phrase for a grant key, with its bound appended when present."""
    return _PHRASES.get(key, key)


def _bound_suffix(value) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    amt = value.get("amount")
    if isinstance(amt, dict) and isinstance(amt.get("max"), (int, float)):
        cur = amt.get("currency", "?")
        win = amt.get("window", "")
        parts.append(f"up to {amt['max']} {cur}" + (f" per {win}" if win else ""))
    rate = value.get("rate")
    if isinstance(rate, dict) and isinstance(rate.get("max"), (int, float)):
        win = rate.get("window", "")
        parts.append(f"at most {rate['max']}" + (f" per {win}" if win else ""))
    if value.get("approval") == "required":
        parts.append("with human approval")
    elif value.get("approval") == "readonly-allowed":
        parts.append("(read-only allowed)")
    return (" " + "; ".join(parts)) if parts else ""


def explain(manifest, *, now=None) -> str:
    """Render a plain-language summary of ``manifest``."""
    now = now or date.today()
    lines = []
    agent = manifest.get("agent", "(unnamed)")
    owner = manifest.get("owner")
    lines.append(f"agent: {agent}" + (f"   owner: {owner}" if owner else ""))
    if manifest.get("description"):
        lines.append(f"  {manifest['description']}")

    ttl_raw = manifest.get("ttl")
    expired = False
    if ttl_raw is not None:
        d = _parse_iso_date(ttl_raw)
        if d is None:
            lines.append(f"ttl: {ttl_raw!r} (unparsable)")
        else:
            expired = d < now
            delta = (d - now).days
            rel = f"{delta} day(s) from now" if delta >= 0 else f"{-delta} day(s) ago"
            lines.append(f"ttl: {d.isoformat()} ({rel}){'  ** EXPIRED **' if expired else ''}")

    grants = manifest.get("grants") if isinstance(manifest.get("grants"), dict) else {}
    deny = manifest.get("deny") if isinstance(manifest.get("deny"), list) else []
    denyset = set(deny)
    denied_systems = {d for d in deny if "." not in d}

    unbounded, dominated = [], []
    may, may_not = [], []

    for key, value in grants.items():
        shadowed = key in denyset or key.split(".")[0] in denied_systems
        phrase = describe(key) + _bound_suffix(value)
        if shadowed:
            dominated.append(key)
            may_not.append(f"{key} (granted but denied — dominated)")
            continue
        may.append(phrase)
        if bound_requirement(key) is not None:
            # check whether the bound is actually satisfied
            satisfied = False
            if isinstance(value, dict):
                if is_money_grant(key):
                    amt = value.get("amount")
                    satisfied = isinstance(amt, dict) and isinstance(amt.get("max"), (int, float))
                elif key in APPROVAL_GATED:
                    satisfied = value.get("approval") == "required"
            if not satisfied:
                unbounded.append(key)

    for d in deny:
        may_not.append(f"{describe(d)} (explicitly denied)")

    if may:
        lines.append("may: " + ", ".join(may))
    else:
        lines.append("may: (nothing granted)")
    if may_not:
        lines.append("may NOT: " + ", ".join(may_not))

    # risk badges
    badges = []
    if unbounded:
        badges.append("unbounded: " + ", ".join(sorted(unbounded)))
    if expired:
        badges.append("expired")
    if dominated:
        badges.append("dominated: " + ", ".join(sorted(dominated)))
    lines.append("badges: " + ("; ".join(badges) if badges else "clean"))

    # surface validator errors so explain doubles as a quick lint
    findings = validate(manifest, now=now)
    errors, _warnings = split_findings(findings)
    if errors:
        lines.append(f"lint: {len(errors)} error(s) — run `manifesto validate`")

    return "\n".join(lines)


def badges(manifest, *, now=None):
    """Return the list of active risk badge strings."""
    now = now or date.today()
    out = []
    grants = manifest.get("grants") if isinstance(manifest.get("grants"), dict) else {}
    deny = set(manifest.get("deny") or [])
    denied_systems = {d for d in deny if "." not in d}
    unbounded = []
    for key, value in grants.items():
        if key in deny or key.split(".")[0] in denied_systems:
            continue
        if bound_requirement(key) is not None:
            satisfied = False
            if isinstance(value, dict):
                if is_money_grant(key):
                    amt = value.get("amount")
                    satisfied = isinstance(amt, dict) and isinstance(amt.get("max"), (int, float))
                elif key in APPROVAL_GATED:
                    satisfied = value.get("approval") == "required"
            if not satisfied:
                unbounded.append(key)
    if unbounded:
        out.append("unbounded")
    d = _parse_iso_date(manifest.get("ttl")) if manifest.get("ttl") else None
    if d is not None and d < now:
        out.append("expired")
    if any((k in deny or k.split(".")[0] in denied_systems) for k in grants):
        out.append("dominated")
    return out
