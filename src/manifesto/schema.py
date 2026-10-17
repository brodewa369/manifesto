"""Schema, mini-YAML parser, and the two-layer validator/linter.

This module is the heart of manifesto. It contains:

* a small, dependency-free YAML-subset parser (``load_yaml``) sufficient for
  ``agent.manifest.yaml`` — maps, lists, inline lists, scalars, comments;
* the manifest registry (``KNOWN``) describing which ``<system>.<verb>`` keys
  are groupable, and which verbs carry an inherent *bound* requirement;
* ``validate()`` — a **two-layer** linter that reports key-paths, human
  explanations, and fix hints:

  1. **Structural layer** — derived from the *published* JSON Schema
     (``schema/agent-manifest.schema.json``) via the tiny draft-07-subset
     engine in :mod:`manifesto.jsonschema_mini`. Types, required fields,
     enums, patterns, and unknown-key rejection all come from the schema
     *file*; mutating that file changes behaviour with no code change
     (see ``tests/test_schema_canary.py``).
  2. **Semantic layer** — registry + business rules that plain JSON Schema
     cannot express: unknown-verb suggestions, bounded-required verbs,
     shadowed grants, ttl date parsing, audit cross-references, and
     delegation attenuation (``delegations`` subset/narrowing).

An **org verb-registry override** (``manifesto validate --registry org.yaml``,
:func:`load_verb_registry`) layers an organisation's own vocabulary on top:
``mode: extend`` adds org systems to :data:`KNOWN` (``mode: replace`` makes the
org file the only authority), ``unknown_verb: error|warning|ignore`` decides
how verbs outside it are reported, and ``risk_classes`` + ``require_approval``
gate chosen classes behind ``approval: required``.

No ``jsonschema`` dependency — pure standard library.

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

from .jsonschema_mini import ERROR, Engine  # noqa: F401  (ERROR re-exported)

# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #

Finding = namedtuple("Finding", ["keypath", "code", "severity", "message", "hint"])

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

#: Approval strictness ordering (higher is stricter) — used by delegation
#: attenuation: a child must be at least as strict as its parent.
APPROVAL_STRICTNESS = {"readonly-allowed": 1, "required": 2}

TOP_LEVEL_KEYS = {"agent", "version", "owner", "description", "ttl",
                  "grants", "deny", "delegations", "audit", "meta"}
GRANT_OBJECT_KEYS = {"amount", "rate", "to_domains", "approval"}
AMOUNT_KEYS = {"max", "currency", "window"}
RATE_KEYS = {"max", "window"}
AUDIT_KEYS = {"receipt_of"}
DELEGATION_KEYS = {"from", "to", "narrower_than", "purpose", "ttl"}

EXAMPLE_NAMESPACE = "gmail.read"

# --------------------------------------------------------------------------- #
# Org verb-registry override (--registry)
# --------------------------------------------------------------------------- #

#: ``mode:`` values for an org registry file.
REGISTRY_MODES = ("extend", "replace")

#: ``unknown_verb:`` rule -> the severity reported for a verb that is outside
#: the effective registry. ``ignore`` maps to ``None``: suppress the finding.
UNKNOWN_RULES = {"error": ERROR, "warning": WARNING, "ignore": None}

#: Top-level keys an org registry file may carry (anything else is a typo).
REGISTRY_KEYS = {"registry", "mode", "unknown_verb", "systems",
                 "risk_classes", "require_approval"}

SYSTEM_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class VerbRegistry:
    """An org-level override of the ``<system>.<verb>`` namespace.

    Built-in registry data (:data:`KNOWN`) is *data*; this is the same idea
    with an org's own verbs on top:

    * ``mode: extend`` (default) — org systems are **added** to the built-ins;
      ``mode: replace`` — only the org's registry is consulted, so a manifest
      must speak the org's closed vocabulary.
    * ``unknown_verb:`` decides what happens to a verb outside the effective
      registry — ``error`` (default, unchanged from a bare `manifesto`),
      ``warning``, or ``ignore``.
    * ``risk_classes:`` maps ``<system>.<verb>`` to an org risk class, and
      ``require_approval:`` names the classes that must be gated with
      ``approval: required`` (finding ``missing_approval``).
    """

    def __init__(self, *, label="", mode="extend", unknown_verb="error",
                 systems=None, risk_classes=None, require_approval=(),
                 path=None):
        self.label = label
        self.mode = mode
        self.unknown_verb = unknown_verb
        self.systems = systems or {}
        self.risk_classes = risk_classes or {}
        self.require_approval = frozenset(require_approval or ())
        self.path = path

    @property
    def unknown_severity(self):
        """Severity for an unknown verb (``None`` = suppressed)."""
        return UNKNOWN_RULES[self.unknown_verb]

    def known(self) -> dict:
        """Effective ``system -> {verbs}`` consulted by the validator."""
        base = {} if self.mode == "replace" else {s: set(v) for s, v in KNOWN.items()}
        for system, verbs in self.systems.items():
            base.setdefault(system, set()).update(verbs)
        return base

    def risk_class(self, key):
        """Org risk class for a grant key, or ``None``."""
        return self.risk_classes.get(key)

    def gated_risk_class(self, key):
        """Risk class that forces ``approval: required``, or ``None``."""
        cls = self.risk_classes.get(key)
        return cls if cls in self.require_approval else None

    def describe(self) -> dict:
        """Machine-readable summary (surfaced by ``validate --json``)."""
        return {
            "label": self.label,
            "mode": self.mode,
            "unknown_verb": self.unknown_verb,
            "systems": sorted(self.systems),
            "risk_classes": len(self.risk_classes),
            "require_approval": sorted(self.require_approval),
            "source": str(self.path) if self.path is not None else None,
        }


def load_verb_registry(path) -> VerbRegistry:
    """Load an org verb-registry file (the manifest YAML subset).

    Shape::

        registry: acme-corp          # optional label
        mode: extend                 # extend (default) | replace
        unknown_verb: error          # error (default) | warning | ignore
        systems:
          acme: [deploy, rollback, status]
        risk_classes:
          acme.deploy: critical
        require_approval: [critical]

    A registry is *authority*, so it is parsed strictly: an unreadable file, a
    non-mapping document, an unknown top-level key, a bad ``mode``/
    ``unknown_verb`` value, or a ``risk_classes`` entry naming a verb the
    registry itself does not know all raise :class:`ManifestError` (CLI exit
    2) rather than being silently skipped. A typo in an authority file must
    never downgrade a manifest to "looks fine".
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read verb registry {p}: {exc}") from exc
    try:
        data = load_yaml(text)
    except Exception as exc:
        raise ManifestError(f"cannot parse verb registry {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{p}: verb registry must be a mapping")

    unknown_keys = {str(k) for k in data} - REGISTRY_KEYS
    if unknown_keys:
        raise ManifestError(
            f"{p}: unknown registry key(s) {', '.join(sorted(unknown_keys))}; "
            f"allowed: {', '.join(sorted(REGISTRY_KEYS))}")

    label = data.get("registry") or ""
    if not isinstance(label, str):
        raise ManifestError(f"{p}: `registry` must be a string label")

    mode = data.get("mode", "extend")
    if mode not in REGISTRY_MODES:
        raise ManifestError(
            f"{p}: `mode` must be one of {', '.join(REGISTRY_MODES)} (got {mode!r})")

    rule = data.get("unknown_verb", "error")
    if rule not in UNKNOWN_RULES:
        raise ManifestError(
            f"{p}: `unknown_verb` must be one of {', '.join(UNKNOWN_RULES)} "
            f"(got {rule!r})")

    raw_systems = data.get("systems") or {}
    if not isinstance(raw_systems, dict):
        raise ManifestError(f"{p}: `systems` must map a system name to a list of verbs")
    systems = {}
    for system, verbs in raw_systems.items():
        if not isinstance(system, str) or not SYSTEM_SEGMENT_RE.match(system):
            raise ManifestError(
                f"{p}: system name {system!r} must be one lowercase segment "
                "(letters, digits, underscore)")
        if not isinstance(verbs, list) or not all(isinstance(v, str) for v in verbs):
            raise ManifestError(
                f"{p}: system '{system}' must list its verbs, e.g. [read, write]")
        systems[system] = tuple(sorted({v.strip() for v in verbs}))

    raw_risk = data.get("risk_classes") or {}
    if not isinstance(raw_risk, dict):
        raise ManifestError(
            f"{p}: `risk_classes` must map <system>.<verb> to a risk class")
    risk_classes = {}
    for key, cls in raw_risk.items():
        if not isinstance(key, str) or not NAMESPACE_RE.match(key):
            raise ManifestError(
                f"{p}: risk class key {key!r} must be <system>.<verb> "
                f"(e.g. {EXAMPLE_NAMESPACE})")
        if not isinstance(cls, str) or not cls.strip():
            raise ManifestError(
                f"{p}: risk class for '{key}' must be a non-empty string")
        risk_classes[key] = cls.strip()

    require = data.get("require_approval") or []
    if not isinstance(require, list) or not all(isinstance(x, str) for x in require):
        raise ManifestError(
            f"{p}: `require_approval` must be a list of risk-class names")

    registry = VerbRegistry(label=label, mode=mode, unknown_verb=rule,
                            systems=systems, risk_classes=risk_classes,
                            require_approval=require, path=p)

    # fail loud: a risk class naming a verb the registry cannot resolve would
    # otherwise be silently dead authority.
    known = registry.known()
    for key in sorted(risk_classes):
        system, _, verb = key.partition(".")
        if system not in known or verb not in known[system]:
            near = _suggest(verb, known.get(system, ()))
            hint = (f"did you mean: {', '.join(near)}?" if near else
                    (f"{system} verbs: {', '.join(sorted(known.get(system, ())))}"
                     if system in known else
                     f"unknown system '{system}' — declare it under `systems:`"))
            raise ManifestError(
                f"{p}: risk class for '{key}' names a verb the registry does "
                f"not know ({hint})")
    return registry


# --------------------------------------------------------------------------- #
# Published JSON Schema (the structural layer's source of truth)
# --------------------------------------------------------------------------- #

#: Locations searched for ``agent-manifest.schema.json``, in order. The first
#: hit wins. Editable/src checkouts find the repo-level ``schema/`` dir;
#: packaged installs find the bundled copy next to this module.
_SCHEMA_RELATIVE = ("schema", "agent-manifest.schema.json")


def schema_path() -> Path:
    """Resolve the published schema document.

    Raises ``ManifestError`` if no schema file can be found — the validator
    refuses to run without its published schema (fail-closed).
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2].joinpath(*_SCHEMA_RELATIVE),   # repo root (src layout)
        here.parent.joinpath(*_SCHEMA_RELATIVE),       # bundled in package
        Path.cwd().joinpath(*_SCHEMA_RELATIVE),        # cwd fallback
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    raise ManifestError(
        "cannot locate agent-manifest.schema.json (looked in: "
        + ", ".join(str(c) for c in candidates) + ")")


def load_schema(path=None) -> dict:
    """Load the published schema document from ``path`` (or the default)."""
    p = Path(path) if path else schema_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read schema {p}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"schema {p} is not valid JSON: {exc}") from exc


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
# Delegation attenuation helper
# --------------------------------------------------------------------------- #

def is_narrower(child, parent) -> bool:
    """True if grant value ``child`` is narrower-than-or-equal to ``parent``.

    Attenuation (inspired by Macaroon/Biscuit caveats): a delegation may only
    *narrow* power, never widen it.

    * parent ``true`` (unbounded)  -> any child value is within scope;
    * child ``true`` over a bounded parent -> widening (escalation) -> False;
    * both objects -> every constraint the parent imposes must be imposed by
      the child at least as strictly (amount/rate caps <=, to_domains a
      subset, approval no weaker). The child may add further constraints.
    """
    if parent is True:
        return True
    if child is True:
        return False  # child unbounded over a bounded parent = escalation
    if not isinstance(child, dict) or not isinstance(parent, dict):
        return False

    # amount cap: child must cap at least as tightly (and must keep the cap)
    p_amt = parent.get("amount")
    if isinstance(p_amt, dict):
        c_amt = child.get("amount")
        if not isinstance(c_amt, dict):
            return False
        p_max, c_max = p_amt.get("max"), c_amt.get("max")
        if _is_number(p_max) and not (_is_number(c_max) and c_max <= p_max):
            return False

    # rate cap
    p_rate = parent.get("rate")
    if isinstance(p_rate, dict):
        c_rate = child.get("rate")
        if not isinstance(c_rate, dict):
            return False
        p_max, c_max = p_rate.get("max"), c_rate.get("max")
        if _is_number(p_max) and not (_is_number(c_max) and c_max <= p_max):
            return False

    # outbound domains: child must be a subset
    p_doms = parent.get("to_domains")
    if isinstance(p_doms, list):
        c_doms = child.get("to_domains")
        if not (isinstance(c_doms, list) and set(c_doms).issubset(set(p_doms))):
            return False

    # approval gate: child must be at least as strict
    p_appr = parent.get("approval")
    if p_appr is not None:
        c_appr = child.get("approval")
        if not (c_appr is not None
                and APPROVAL_STRICTNESS.get(c_appr, 0) >= APPROVAL_STRICTNESS.get(p_appr, 0)):
            return False

    return True


# --------------------------------------------------------------------------- #
# Validator (structural layer derived from schema + semantic layer)
# --------------------------------------------------------------------------- #

def validate(manifest, *, now=None, schema_path_=None, registry=None):
    """Lint a loaded manifest. Returns a list of :class:`Finding` (sorted).

    ``schema_path_`` optionally points at an alternate schema document (used by
    the canary test to prove behaviour derives from the schema *file*).
    ``registry`` optionally supplies an org :class:`VerbRegistry` override
    (``--registry``): it decides which ``<system>.<verb>`` keys are known and
    with which severity unknown ones are reported, and adds the risk-class
    approval rule. With ``registry=None`` behaviour is byte-identical to a bare
    ``manifesto validate``.
    """
    now = now or date.today()
    findings: list[Finding] = []

    # ---- structural layer: derived from the published schema ------------- #
    schema = load_schema(schema_path_)
    for sf in Engine(schema).validate(manifest):
        findings.append(Finding(sf.keypath, sf.code, sf.severity, sf.message, sf.hint))

    # ---- semantic layer: registry + business rules ----------------------- #
    if isinstance(manifest, dict):
        _semantic(manifest, now, findings, registry)

    findings.sort(key=lambda f: (f.keypath, f.code, f.message))
    return findings


def _semantic(manifest, now, findings, registry=None):
    def add(keypath, code, severity, message, hint=""):
        findings.append(Finding(keypath, code, severity, message, hint))

    # effective verb namespace + the rule for verbs outside it
    known = registry.known() if registry is not None else KNOWN
    unknown_severity = registry.unknown_severity if registry is not None else ERROR

    # ---- ttl (date semantics; structural layer only checks it is a string) #
    ttl_raw = manifest.get("ttl")
    if ttl_raw is None:
        add("ttl", "missing_ttl", WARNING,
            "no `ttl` set — grants never expire on their own",
            "add `ttl: YYYY-MM-DD` (ISO date) so grants lapse")
    elif isinstance(ttl_raw, str):
        ttl_date = _parse_iso_date(ttl_raw)
        if ttl_date is None:
            add("ttl", "bad_ttl", ERROR, f"'ttl' is not an ISO date: {ttl_raw!r}",
                "use `ttl: 2027-01-01`")
        elif ttl_date < now:
            add("ttl", "expired", WARNING,
                f"ttl {ttl_date.isoformat()} is in the past — the contract has expired",
                "bump `ttl` or retire this agent")
    # non-string ttl is already a structural bad_type; nothing more to do here.

    # ---- deny (registry lookups for shadow set) --------------------------- #
    deny = manifest.get("deny")
    deny_namespaces = set()
    if isinstance(deny, list):
        for i, entry in enumerate(deny):
            if not isinstance(entry, str):
                continue  # structural bad_type already emitted for non-strings
            kp = f"deny[{i}]"
            if "." not in entry:
                if entry in known:
                    deny_namespaces.add(entry)
                elif unknown_severity is not None:
                    add(kp, "unknown_verb", unknown_severity,
                        f"unknown system '{entry}' in deny",
                        f"did you mean one of: {', '.join(sorted(known))[:80]}?")
            elif not NAMESPACE_RE.match(entry):
                add(kp, "ungroupable_verb", ERROR,
                    f"'{entry}' is not a valid <system>.<verb> key",
                    f"use `<system>.<verb>`, e.g. {EXAMPLE_NAMESPACE}")
            else:
                system, _, verb = entry.partition(".")
                _check_namespace(kp, system, verb, add, known, unknown_severity)
                deny_namespaces.add(entry)

    # ---- grants (registry, bounds, shadow) -------------------------------- #
    grants = manifest.get("grants")
    grant_keys = []
    if isinstance(grants, dict):
        for key, value in grants.items():
            kp = f"grants.{key}"
            if not isinstance(key, str) or not NAMESPACE_RE.match(key):
                continue  # structural propertyNames already flagged ungroupable
            system, _, verb = key.partition(".")
            if _check_namespace(kp, system, verb, add, known, unknown_severity) is False:
                continue
            grant_keys.append(key)

            # shadow check: grant whose parent (or itself) is denied
            if key in deny_namespaces or system in deny_namespaces:
                shadowed_by = key if key in deny_namespaces else system
                add(kp, "shadowed_grant", ERROR,
                    f"grant '{key}' is shadowed by deny '{shadowed_by}' — "
                    "the deny dominates, so this grant is dead",
                    "remove the conflicting deny entry, or drop this grant")

            _validate_grant_value(kp, key, value, add, registry)

    # ---- audit ------------------------------------------------------------- #
    audit = manifest.get("audit")
    if isinstance(audit, dict):
        for key in audit:
            if key not in AUDIT_KEYS:
                add(f"audit.{key}", "unknown_key", ERROR,
                    f"unknown audit key '{key}'",
                    f"allowed: {', '.join(sorted(AUDIT_KEYS))}")
        receipts = audit.get("receipt_of")
        if isinstance(receipts, list):
            granted = set(grant_keys)
            for i, r in enumerate(receipts):
                if r not in granted:
                    add(f"audit.receipt_of[{i}]", "audit_unknown_grant", WARNING,
                        f"audit asks for receipts of '{r}' which is not a granted key",
                        "reference an existing grant key")

    # ---- delegations (attenuation: child ⊆ parent, narrowed) -------------- #
    _check_delegations(manifest, grant_keys, grants, add)


def _check_delegations(manifest, grant_keys, grants, add):
    delegations = manifest.get("delegations")
    if not isinstance(delegations, list):
        return
    parent_agent = manifest.get("agent")
    parent_grants = grants if isinstance(grants, dict) else {}

    for i, d in enumerate(delegations):
        if not isinstance(d, dict):
            continue  # structural bad_type already emitted
        kp = f"delegations[{i}]"
        frm = d.get("from")
        child = d.get("narrower_than")

        # the delegation must be resolvable against THIS contract as parent
        if isinstance(frm, str) and isinstance(parent_agent, str) and frm != parent_agent:
            add(f"{kp}.from", "unknown_parent", ERROR,
                f"delegation from '{frm}' cannot be resolved against this "
                f"contract's agent '{parent_agent}'",
                "set `from` to this contract's agent so attenuation is checkable")
            continue

        if not isinstance(child, dict):
            continue  # structural bad_type already emitted for non-object

        for key, cval in child.items():
            gkp = f"{kp}.narrower_than.{key}"
            if not isinstance(key, str) or not NAMESPACE_RE.match(key):
                continue  # structural propertyNames already flagged ungroupable
            if key not in parent_grants:
                add(gkp, "delegation_escalation", ERROR,
                    f"delegated grant '{key}' is not granted to the delegating "
                    f"agent '{parent_agent}'",
                    "delegate only a subset of the parent's grants")
                continue
            if not is_narrower(cval, parent_grants[key]):
                add(gkp, "delegation_escalation", ERROR,
                    f"delegated grant '{key}' is not narrower than the parent's "
                    "grant — a delegation may only narrow power, never widen it",
                    "tighten the child's constraints to <= the parent's, or drop it")


def _check_namespace(keypath, system, verb, add, known=None,
                     unknown_severity=ERROR) -> bool:
    """Validate a ``<system>.<verb>`` pair; return False if it is unknown.

    ``known`` is the effective registry (org override applied); with
    ``unknown_severity=None`` the unknown-verb finding is suppressed entirely
    (``unknown_verb: ignore``) but the key still does not validate as known.
    """
    if known is None:
        known = KNOWN
    if system not in known:
        if unknown_severity is None:
            return False
        near = _suggest(system, known.keys())
        hint = f"did you mean one of: {', '.join(near)}?" if near else \
            f"known systems: {', '.join(sorted(known))}"
        add(keypath, "unknown_verb", unknown_severity,
            f"unknown system '{system}' (verb '{verb}')", hint)
        return False
    if verb not in known[system]:
        if unknown_severity is None:
            return False
        near = _suggest(verb, known[system])
        hint = f"did you mean: {', '.join(near)}?" if near else \
            f"{system} verbs: {', '.join(sorted(known[system]))}"
        add(keypath, "unknown_verb", unknown_severity,
            f"unknown verb '{verb}' for system '{system}'", hint)
        return False
    return True


def _validate_grant_value(keypath, key, value, add, registry=None):
    req = bound_requirement(key)

    if value is False:
        add(keypath, "grant_false", WARNING,
            f"grant '{key}' is set to false — use `deny:` to express refusal",
            f"move to `deny: [{key}]`")
        return

    # org registry rule: a risk class the org gates behind human approval
    gated_class = registry.gated_risk_class(key) if registry is not None else None
    if gated_class is not None and (value is True or
                                    (isinstance(value, dict) and value)):
        if not (isinstance(value, dict) and value.get("approval") == "required"):
            add(keypath, "missing_approval", ERROR,
                f"grant '{key}' is risk class '{gated_class}' which the org "
                "registry gates with `approval: required`",
                "add `approval: required`, or move the class out of the "
                "registry's `require_approval` list")

    if value is True:
        if req is not None:
            bound_field, why = req
            add(keypath, "unbounded_grant", ERROR,
                f"unbounded grant '{key}' ({why})",
                f"give it a bound, e.g. `{key}: {{ {bound_field}: ... }}`")
        return

    if not isinstance(value, dict):
        # structural bad_type already covers this; keep semantic bail-out safe
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
        if isinstance(amt, dict):
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
        if isinstance(rate, dict):
            if "max" not in rate or not _is_number(rate.get("max")):
                add(f"{kp}.max", "unbounded_grant", ERROR,
                    f"grant '{key}' has a rate with no numeric `max` — still unbounded",
                    "set `rate.max` to a number")
            if "window" not in rate or not isinstance(rate.get("window"), str):
                add(f"{kp}.window", "missing_window", WARNING,
                    f"grant '{key}' rate has no time window", "e.g. `window: P1H`")

    # bound requirement met? (to_domains/approval structural types already checked)
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
