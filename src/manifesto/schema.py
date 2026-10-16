"""Schema, mini-YAML parser, and the hand-rolled validator/linter.

This module is the heart of manifesto. It contains:

* a small, dependency-free YAML-subset parser (``load_yaml``) sufficient for
  ``agent.manifest.yaml`` — maps, lists, inline lists, scalars, comments;
* the manifest registry (``KNOWN``) describing which ``<system>.<verb>`` keys
  are groupable, and which verbs carry an inherent *bound* requirement;
* ``validate()`` — a hand-rolled linter (deliberately **no** ``jsonschema``
  dependency) that reports key-paths, human explanations, and fix hints.

Findings carry a ``severity``:

* ``error``   — the manifest is unsound; ``manifesto validate`` exits 1.
* ``warning`` — suspicious but not fatal; only fails under ``--strict``.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import namedtuple
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #

Finding = namedtuple("Finding", ["keypath", "code", "severity", "message", "hint"])

ERROR = "error"
WARNING = "warning"


class ManifestError(Exception):
    """Raised when a manifest file cannot be parsed at all."""


# --------------------------------------------------------------------------- #
# Registry: which <system>.<verb> keys are groupable
# --------------------------------------------------------------------------- #

#: system -> set of verbs that system exposes. Anything outside this is an
#: "unknown verb" finding (with a suggestion). Extend freely; it is data.
KNOWN = {
    "gmail":     {"read", "send", "draft", "delete", "search", "label", "modify"},
    "calendar":  {"read", "list", "create", "update", "delete"},
    "contacts":  {"read", "write", "delete"},
    "payments":  {"read", "charge", "refund", "payout"},
    "fs":        {"read", "write", "list", "move", "delete"},
    "files":     {"read", "write", "list", "delete"},
    "shell":     {"exec"},
    "http":      {"get", "post", "put", "delete", "request"},
    "browser":   {"navigate", "read", "click", "type"},
    "slack":     {"read", "send", "delete"},
    "notion":    {"read", "write", "delete"},
    "github":    {"read", "write", "merge", "delete"},
    "db":        {"read", "write", "delete"},
    "drive":     {"read", "write", "delete"},
    "sheets":    {"read", "write"},
    "jira":      {"read", "write"},
    "sms":       {"read", "send"},
    "discord":   {"read", "send"},
    "webhook":   {"post"},
}

#: The verb-namespace convention: exactly ``<system>.<verb>``, lowercase.
NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Systems whose verbs move money — a bare grant of these is unbounded.
MONEY_SYSTEMS = {"payments"}
MONEY_VERBS = {"charge", "refund", "payout"}

#: Verbs that are inherently unbounded unless gated by human approval.
APPROVAL_GATED = {"shell.exec"}

APPROVAL_MODES = {"required", "readonly-allowed"}

TOP_LEVEL_KEYS = {"agent", "version", "owner", "description", "ttl",
                  "grants", "deny", "audit", "meta"}
GRANT_OBJECT_KEYS = {"amount", "rate", "to_domains", "approval"}
AMOUNT_KEYS = {"max", "currency", "window"}
RATE_KEYS = {"max", "window"}
AUDIT_KEYS = {"receipt_of"}

EXAMPLE_NAMESPACE = "gmail.read"


# --------------------------------------------------------------------------- #
# Mini-YAML parser (YAML subset — enough for agent.manifest.yaml)
# --------------------------------------------------------------------------- #

def _strip_comment(line: str) -> str:
    out = []
    in_s = in_d = False
    for ch in line:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and (not out or out[-1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _split_flow(inner: str):
    parts, buf = [], ""
    in_s = in_d = False
    for ch in inner:
        if ch == "'" and not in_d:
            in_s = not in_s
            buf += ch
        elif ch == '"' and not in_s:
            in_d = not in_d
            buf += ch
        elif ch == "," and not in_s and not in_d:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip() != "" or (parts and buf != ""):
        parts.append(buf.strip())
    return parts


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _scalar(text: str):
    text = text.strip()
    if text == "":
        return None
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(p) for p in _split_flow(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        out = {}
        for part in _split_flow(text[1:-1]):
            if ":" in part:
                k, _, v = part.partition(":")
                out[_unquote(k.strip())] = _scalar(v)
        return out
    if text[0] in ("'", '"') and text[-1] == text[0]:
        return _unquote(text)
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class _Parser:
    def __init__(self, lines):
        self.lines = lines
        self.pos = 0

    def peek(self):
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def parse(self):
        item = self.peek()
        if item is None:
            return None
        return self._node(item[0])

    def _node(self, indent):
        item = self.peek()
        if item is None or item[0] < indent:
            return None
        if item[1].startswith("- "):
            return self._list(item[0])
        return self._map(item[0])

    def _map(self, indent):
        result = {}
        while True:
            item = self.peek()
            if item is None or item[0] != indent or item[1].startswith("- "):
                break
            _, text = item
            key, _, rest = text.partition(":")
            key = key.strip()
            rest = rest.strip()
            self.pos += 1
            if rest == "":
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent:
                    result[key] = self._node(nxt[0])
                else:
                    result[key] = None
            else:
                result[key] = _scalar(rest)
        return result

    def _list(self, indent):
        result = []
        while True:
            item = self.peek()
            if item is None or item[0] != indent or not item[1].startswith("- "):
                break
            _, text = item
            content = text[2:].strip()
            self.pos += 1
            if content == "":
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent:
                    result.append(self._node(nxt[0]))
                else:
                    result.append(None)
            elif content.endswith(":") or ": " in content:
                if content.endswith(":"):
                    key, rest = content[:-1].strip(), ""
                else:
                    key, _, rest = content.partition(": ")
                    key, rest = key.strip(), rest.strip()
                entry = {}
                if rest == "":
                    nxt = self.peek()
                    if nxt is not None and nxt[0] > indent:
                        entry[key] = self._node(nxt[0])
                    else:
                        entry[key] = None
                else:
                    entry[key] = _scalar(rest)
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent and not nxt[1].startswith("- "):
                    entry.update(self._map(nxt[0]))
                result.append(entry)
            else:
                result.append(_scalar(content))
        return result


def load_yaml(text: str):
    """Parse a YAML-subset document into Python objects."""
    lines = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))
    return _Parser(lines).parse()


def load_manifest_file(path):
    """Load a manifest from disk. JSON if it ends in .json, YAML otherwise."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {p}: {exc}") from exc
    try:
        if p.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            data = load_yaml(text)
    except ManifestError:
        raise
    except Exception as exc:  # parse failure of any kind
        raise ManifestError(f"cannot parse {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{p}: top level must be a mapping, got {type(data).__name__}")
    return data


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _parse_iso_date(s):
    """Return a ``date`` from an ISO string, or ``None`` if unparsable."""
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    if not isinstance(s, str):
        return None
    s = s.strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def is_money_grant(key: str) -> bool:
    system, _, verb = key.partition(".")
    return system in MONEY_SYSTEMS and verb in MONEY_VERBS


def bound_requirement(key: str):
    """Return a human description if ``key`` inherently requires a bound."""
    if is_money_grant(key):
        return ("amount.max", "money-moving grant needs amount.max with currency and window")
    if key in APPROVAL_GATED:
        return ("approval", "shell.exec is unbounded by nature; require approval: required")
    return None


def _suggest(word, pool):
    return difflib.get_close_matches(word, sorted(pool), n=3, cutoff=0.5)


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #

def validate(manifest, *, now=None):
    """Lint a loaded manifest. Returns a list of :class:`Finding` (sorted)."""
    now = now or date.today()
    findings: list[Finding] = []

    def add(keypath, code, severity, message, hint=""):
        findings.append(Finding(keypath, code, severity, message, hint))

    if not isinstance(manifest, dict):
        add("", "bad_type", ERROR, "manifest root must be a mapping", "start with `agent: <name>`")
        return findings

    # ---- top-level unknown keys ------------------------------------------- #
    for key in manifest:
        if key not in TOP_LEVEL_KEYS:
            add(key, "unknown_key", ERROR,
                f"unknown top-level key '{key}'",
                f"allowed: {', '.join(sorted(TOP_LEVEL_KEYS))}")

    # ---- agent (required) -------------------------------------------------- #
    agent = manifest.get("agent")
    if agent is None or (isinstance(agent, str) and not agent.strip()):
        add("agent", "missing_agent", ERROR,
            "required field 'agent' is missing or empty",
            "add `agent: my-agent-name`")
    elif not isinstance(agent, str):
        add("agent", "bad_type", ERROR, "'agent' must be a string",
            "e.g. `agent: sales-ops-bot`")

    # ---- ttl --------------------------------------------------------------- #
    ttl_raw = manifest.get("ttl")
    if ttl_raw is None:
        add("ttl", "missing_ttl", WARNING,
            "no `ttl` set — grants never expire on their own",
            "add `ttl: YYYY-MM-DD` (ISO date) so grants lapse")
    else:
        ttl_date = _parse_iso_date(ttl_raw)
        if ttl_date is None:
            add("ttl", "bad_ttl", ERROR, f"'ttl' is not an ISO date: {ttl_raw!r}",
                "use `ttl: 2027-01-01`")
        elif ttl_date < now:
            add("ttl", "expired", WARNING,
                f"ttl {ttl_date.isoformat()} is in the past — the contract has expired",
                "bump `ttl` or retire this agent")

    # ---- deny -------------------------------------------------------------- #
    deny = manifest.get("deny")
    deny_namespaces = set()
    if deny is not None:
        if not isinstance(deny, list):
            add("deny", "bad_type", ERROR, "'deny' must be a list",
                "e.g. `deny: [shell.exec, payments.charge]`")
            deny = []
        for i, entry in enumerate(deny):
            kp = f"deny[{i}]"
            if not isinstance(entry, str):
                add(kp, "bad_type", ERROR, "deny entries must be strings", EXAMPLE_NAMESPACE)
                continue
            if "." not in entry:
                # a bare system namespace is allowed in deny
                if entry in KNOWN:
                    deny_namespaces.add(entry)
                else:
                    add(kp, "unknown_verb", ERROR,
                        f"unknown system '{entry}' in deny",
                        f"did you mean one of: {', '.join(sorted(KNOWN))[:80]}?")
            elif not NAMESPACE_RE.match(entry):
                add(kp, "ungroupable_verb", ERROR,
                    f"'{entry}' is not a valid <system>.<verb> key",
                    f"use `<system>.<verb>`, e.g. {EXAMPLE_NAMESPACE}")
            else:
                system, _, verb = entry.partition(".")
                _check_namespace(kp, system, verb, add)
                deny_namespaces.add(entry)

    # ---- grants ------------------------------------------------------------ #
    grants = manifest.get("grants")
    grant_keys = []
    if grants is None:
        add("grants", "missing_grants", WARNING, "no `grants` declared — nothing is permitted",
            "add a `grants:` map, e.g. `gmail.read: true`")
    elif not isinstance(grants, dict):
        add("grants", "bad_type", ERROR, "'grants' must be a mapping of grant-key -> true|object",
            "e.g. `grants:\n  gmail.read: true`")
    else:
        for key, value in grants.items():
            kp = f"grants.{key}"
            if not isinstance(key, str) or not NAMESPACE_RE.match(key):
                if isinstance(key, str) and key.count(".") != 1:
                    add(kp, "ungroupable_verb", ERROR,
                        f"'{key}' is not a groupable <system>.<verb> key",
                        "the verb namespace is exactly two segments: `<system>.<verb>`, "
                        f"e.g. {EXAMPLE_NAMESPACE}")
                    continue
                add(kp, "ungroupable_verb", ERROR,
                    f"'{key}' is not a valid <system>.<verb> key",
                    f"use lowercase `<system>.<verb>`, e.g. {EXAMPLE_NAMESPACE}")
                continue
            system, _, verb = key.partition(".")
            if _check_namespace(kp, system, verb, add) is False:
                continue
            grant_keys.append(key)

            # shadow check: grant whose parent (or itself) is denied
            if key in deny_namespaces or system in deny_namespaces:
                shadowed_by = key if key in deny_namespaces else system
                add(kp, "shadowed_grant", ERROR,
                    f"grant '{key}' is shadowed by deny '{shadowed_by}' — "
                    "the deny dominates, so this grant is dead",
                    "remove the conflicting deny entry, or drop this grant")

            _validate_grant_value(kp, key, value, add)

    # ---- audit ------------------------------------------------------------- #
    audit = manifest.get("audit")
    if audit is not None:
        if not isinstance(audit, dict):
            add("audit", "bad_type", ERROR, "'audit' must be a mapping",
                "e.g. `audit:\n  receipt_of: [payments.charge]`")
        else:
            for key in audit:
                if key not in AUDIT_KEYS:
                    add(f"audit.{key}", "unknown_key", ERROR,
                        f"unknown audit key '{key}'",
                        f"allowed: {', '.join(sorted(AUDIT_KEYS))}")
            receipts = audit.get("receipt_of")
            if receipts is not None:
                if not isinstance(receipts, list):
                    add("audit.receipt_of", "bad_type", ERROR,
                        "'audit.receipt_of' must be a list of grant keys",
                        "e.g. `receipt_of: [payments.charge]`")
                else:
                    granted = set(grant_keys)
                    for i, r in enumerate(receipts):
                        if r not in granted:
                            add(f"audit.receipt_of[{i}]", "audit_unknown_grant", WARNING,
                                f"audit asks for receipts of '{r}' which is not a granted key",
                                "reference an existing grant key")

    findings.sort(key=lambda f: (f.keypath, f.code, f.message))
    return findings


def _check_namespace(keypath, system, verb, add) -> bool:
    """Validate a ``<system>.<verb>`` pair; return False if it is unknown."""
    if system not in KNOWN:
        near = _suggest(system, KNOWN.keys())
        hint = f"did you mean one of: {', '.join(near)}?" if near else \
            f"known systems: {', '.join(sorted(KNOWN))}"
        add(keypath, "unknown_verb", ERROR,
            f"unknown system '{system}' (verb '{verb}')", hint)
        return False
    if verb not in KNOWN[system]:
        near = _suggest(verb, KNOWN[system])
        hint = f"did you mean: {', '.join(near)}?" if near else \
            f"{system} verbs: {', '.join(sorted(KNOWN[system]))}"
        add(keypath, "unknown_verb", ERROR,
            f"unknown verb '{verb}' for system '{system}'", hint)
        return False
    return True


def _validate_grant_value(keypath, key, value, add):
    req = bound_requirement(key)

    if value is False:
        add(keypath, "grant_false", WARNING,
            f"grant '{key}' is set to false — use `deny:` to express refusal",
            f"move to `deny: [{key}]`")
        return

    if value is True:
        if req is not None:
            bound_field, why = req
            add(keypath, "unbounded_grant", ERROR,
                f"unbounded grant '{key}' ({why})",
                f"give it a bound, e.g. `{key}: {{ {bound_field}: ... }}`")
        return

    if not isinstance(value, dict):
        add(keypath, "bad_type", ERROR,
            f"grant '{key}' must be `true` or a constraint object, got {type(value).__name__}",
            f"use `true` or a map with keys: {', '.join(sorted(GRANT_OBJECT_KEYS))}")
        return

    if not value:
        add(keypath, "empty_grant", ERROR,
            f"grant '{key}' is an empty object — it grants nothing and hides intent",
            f"write `{key}: true` (unbounded) or add constraints like amount/rate/approval")
        return

    for sub in value:
        if sub not in GRANT_OBJECT_KEYS:
            add(f"{keypath}.{sub}", "unknown_key", ERROR,
                f"unknown constraint '{sub}' on grant '{key}'",
                f"allowed: {', '.join(sorted(GRANT_OBJECT_KEYS))}")

    # amount
    if "amount" in value:
        amt = value["amount"]
        kp = f"{keypath}.amount"
        if not isinstance(amt, dict):
            add(kp, "bad_type", ERROR, "'amount' must be a mapping with max/currency/window",
                "e.g. `amount: { max: 500, currency: USD, window: P1D }`")
        else:
            for sub in amt:
                if sub not in AMOUNT_KEYS:
                    add(f"{kp}.{sub}", "unknown_key", ERROR,
                        f"unknown amount key '{sub}'", f"allowed: {', '.join(sorted(AMOUNT_KEYS))}")
            if "max" not in amt or not _is_number(amt.get("max")):
                add(f"{kp}.max", "unbounded_grant", ERROR,
                    f"grant '{key}' has an amount with no numeric `max` — still unbounded",
                    "set `amount.max` to a number")
            if "currency" not in amt or not isinstance(amt.get("currency"), str):
                add(f"{kp}.currency", "missing_currency", WARNING,
                    f"grant '{key}' amount has no currency", "e.g. `currency: USD`")
            if "window" not in amt or not isinstance(amt.get("window"), str):
                add(f"{kp}.window", "missing_window", WARNING,
                    f"grant '{key}' amount has no time window", "e.g. `window: P1D` (one day)")

    # rate
    if "rate" in value:
        rate = value["rate"]
        kp = f"{keypath}.rate"
        if not isinstance(rate, dict):
            add(kp, "bad_type", ERROR, "'rate' must be a mapping with max/window",
                "e.g. `rate: { max: 10, window: P1H }`")
        else:
            for sub in rate:
                if sub not in RATE_KEYS:
                    add(f"{kp}.{sub}", "unknown_key", ERROR,
                        f"unknown rate key '{sub}'", f"allowed: {', '.join(sorted(RATE_KEYS))}")
            if "max" not in rate or not _is_number(rate.get("max")):
                add(f"{kp}.max", "unbounded_grant", ERROR,
                    f"grant '{key}' has a rate with no numeric `max` — still unbounded",
                    "set `rate.max` to a number")
            if "window" not in rate or not isinstance(rate.get("window"), str):
                add(f"{kp}.window", "missing_window", WARNING,
                    f"grant '{key}' rate has no time window", "e.g. `window: P1H`")

    # to_domains
    if "to_domains" in value:
        doms = value["to_domains"]
        kp = f"{keypath}.to_domains"
        if not isinstance(doms, list) or not all(isinstance(d, str) for d in doms):
            add(kp, "bad_type", ERROR, "'to_domains' must be a list of domain strings",
                "e.g. `to_domains: [example.com]`")

    # approval
    if "approval" in value:
        appr = value["approval"]
        if appr not in APPROVAL_MODES:
            add(f"{keypath}.approval", "bad_approval", ERROR,
                f"approval must be one of {sorted(APPROVAL_MODES)}, got {appr!r}",
                "use `approval: required` or `approval: readonly-allowed`")

    # bound requirement met?
    if req is not None:
        bound_field, why = req
        satisfied = False
        if bound_field == "amount.max":
            amt = value.get("amount")
            satisfied = isinstance(amt, dict) and _is_number(amt.get("max"))
        elif bound_field == "approval":
            satisfied = value.get("approval") == "required"
        if not satisfied:
            add(keypath, "unbounded_grant", ERROR,
                f"unbounded grant '{key}' ({why})",
                f"give it a bound, e.g. `{key}: {{ {bound_field.replace('.', ': ')}: ... }}`")


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #

def split_findings(findings):
    """Return ``(errors, warnings)`` lists."""
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    return errors, warnings
