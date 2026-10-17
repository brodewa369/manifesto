"""``manifesto diff`` — compare two contracts and gate loosening.

A contract that only ever grows is a governance bug you find out about in an
incident, not in review. ``manifesto diff old.yaml new.yaml`` answers the PR
question directly: *which grants were added, removed, tightened or loosened,
what happened to the expiry, and did any delegation change?*

Classification (documented in SPEC.md, Contract diff):

* **tightened** — less power: a cap lowered, a domain list narrowed, an
  approval gate added or made stricter, a grant or delegation dropped, a
  receipt requirement added, an expiry moved earlier.
* **loosened**  — more power: a cap raised, a constraint or approval gate
  dropped, targets widened, a grant or delegation added, a ``deny:`` entry
  removed, a receipt requirement dropped, an expiry moved later (or removed).
* **neutral**   — reported, direction unknown (e.g. an amount *window* change,
  where tightening depends on the enforcer's period semantics).

**The loosening gate.** A loosening change is *gated* when it touches a
**sensitive class** (``payments``, ``fs``/``files``, ``shell``) or a
**previously explicitly-denied** key/namespace. ``--strict`` exits 1 on a
gated loosening unless ``--allow-loosening`` is passed. Loosening outside
those areas is still reported — it just does not fail the build.

Exit codes: ``0`` no gated loosening, ``1`` gated loosening under ``--strict``,
``2`` usage error or unreadable manifest.
"""
from __future__ import annotations

import json
from collections import namedtuple

from .schema import APPROVAL_STRICTNESS, ManifestError, _parse_iso_date, \
    load_manifest_file

#: Grant key -> sensitive class. ``files`` is treated as part of the ``fs``
#: class: it is the same capability surface under a different system name.
SENSITIVE_CLASSES = {"payments": "payments", "fs": "fs", "files": "fs",
                     "shell": "shell"}

GROUP_ORDER = ("grant", "deny", "expiry", "delegation", "audit")

#: One line of the report. ``direction`` is ``tightened`` | ``loosened`` |
#: ``neutral``; ``sensitive`` is the sensitive class (or ``""``);
#: ``was_denied`` marks an area that ``old`` explicitly denied.
Change = namedtuple("Change",
                    ["group", "area", "kind", "direction", "detail",
                     "sensitive", "was_denied"])


class DiffError(ManifestError):
    """Raised when a diff input cannot be compared."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _deny_sets(manifest):
    """Return ``(exact_keys, namespace_keys)`` from a manifest's ``deny:``."""
    deny = manifest.get("deny")
    exact, namespaces = set(), set()
    if isinstance(deny, list):
        for entry in deny:
            if not isinstance(entry, str):
                continue
            if "." in entry:
                exact.add(entry)
            else:
                namespaces.add(entry)
    return exact, namespaces


def _was_denied(key, deny_exact, deny_ns) -> bool:
    system = str(key).partition(".")[0]
    return key in deny_exact or system in deny_ns


def _sensitive_of(keys) -> str:
    """Sensitive class shared by ``keys`` (``""`` when none applies)."""
    found = sorted({SENSITIVE_CLASSES.get(str(k).partition(".")[0], "")
                    for k in keys} - {""})
    return ",".join(found)


def _grants(manifest):
    grants = manifest.get("grants")
    return grants if isinstance(grants, dict) else {}


def _delegations(manifest):
    dels = manifest.get("delegations")
    if not isinstance(dels, list):
        return {}
    out = {}
    for d in dels:
        if isinstance(d, dict):
            out[(str(d.get("from")), str(d.get("to")))] = d
    return out


def _val_desc(value) -> str:
    """Short human description of a grant value (for added/removed rows)."""
    if value is True:
        return "unbounded"
    if not isinstance(value, dict) or not value:
        return repr(value)
    parts = []
    amt = value.get("amount")
    if isinstance(amt, dict) and _is_number(amt.get("max")):
        bit = f"amount<={amt['max']}"
        if amt.get("currency"):
            bit += f" {amt['currency']}"
        if amt.get("window"):
            bit += f"/{amt['window']}"
        parts.append(bit)
    rate = value.get("rate")
    if isinstance(rate, dict) and _is_number(rate.get("max")):
        bit = f"rate<={rate['max']}"
        if rate.get("window"):
            bit += f"/{rate['window']}"
        parts.append(bit)
    doms = value.get("to_domains")
    if isinstance(doms, list) and doms:
        parts.append(f"targets={len(doms)}")
    if value.get("approval"):
        parts.append(f"approval={value['approval']}")
    return ", ".join(parts) if parts else "constraints"


# --------------------------------------------------------------------------- #
# Constraint comparison (shared by grants and delegations)
# --------------------------------------------------------------------------- #

def _compare_cap(old_block, new_block, label, add):
    """Compare an ``amount``/``rate`` cap block; ``label`` like ``amount.max``."""
    old_max = old_block.get("max") if isinstance(old_block, dict) else None
    new_max = new_block.get("max") if isinstance(new_block, dict) else None
    has_old = isinstance(old_block, dict)
    has_new = isinstance(new_block, dict)
    if has_old and has_new:
        if _is_number(old_max) and _is_number(new_max):
            if new_max > old_max:
                add("loosened", f"{label} {old_max} -> {new_max}")
            elif new_max < old_max:
                add("tightened", f"{label} {old_max} -> {new_max}")
        elif _is_number(old_max) and not _is_number(new_max):
            add("loosened", f"{label} dropped (was {old_max})")
        elif not _is_number(old_max) and _is_number(new_max):
            add("tightened", f"{label} added ({new_max})")
    elif has_old and not has_new:
        add("loosened", f"{label} cap dropped")
    elif not has_old and has_new:
        add("tightened", f"{label} cap added")


def _compare_value(old, new):
    """Compare two grant values -> ``(direction, [detail, ...])``."""
    deltas = []
    state = {"loosened": False, "tightened": False}

    def add(direction, text):
        deltas.append(text)
        if direction in state:
            state[direction] = True

    if isinstance(old, dict) and isinstance(new, dict):
        _compare_cap(old.get("amount"), new.get("amount"), "amount.max", add)
        _compare_cap(old.get("rate"), new.get("rate"), "rate.max", add)

        for block_name in ("amount", "rate"):
            ob = old.get(block_name)
            nb = new.get(block_name)
            if isinstance(ob, dict) and isinstance(nb, dict):
                for field in ("currency", "window"):
                    ov, nv = ob.get(field), nb.get(field)
                    if ov != nv:
                        add("neutral", f"{block_name}.{field} {ov} -> {nv}")

        od = old.get("to_domains")
        nd = new.get("to_domains")
        has_od = isinstance(od, list) and bool(od)
        has_nd = isinstance(nd, list) and bool(nd)
        if has_od and has_nd:
            oset, nset = set(map(str, od)), set(map(str, nd))
            added, dropped = sorted(nset - oset), sorted(oset - nset)
            if added:
                # any newly reachable target widens the surface -> loosening
                text = "to_domains widened (+" + ", ".join(added)
                if dropped:
                    text += "; -" + ", ".join(dropped)
                add("loosened", text + ")")
            elif dropped:
                add("tightened", "to_domains narrowed (-" + ", ".join(dropped) + ")")
        elif has_od and not has_nd:
            add("loosened", "to_domains dropped (targets unrestricted)")
        elif has_nd and not has_od:
            add("tightened", f"to_domains added ({len(set(map(str, nd)))} target(s))")

        oa, na = old.get("approval"), new.get("approval")
        if oa != na:
            if oa is not None and na is None:
                add("loosened", "approval gate dropped")
            elif oa is None and na is not None:
                add("tightened", f"approval gate added ({na})")
            else:
                so = APPROVAL_STRICTNESS.get(oa, 0)
                sn = APPROVAL_STRICTNESS.get(na, 0)
                direction = "loosened" if sn < so else ("tightened" if sn > so else "neutral")
                add(direction, f"approval {oa} -> {na}")
    elif old is True and isinstance(new, dict):
        add("tightened", "constraints added (was unbounded)")
    elif new is True and isinstance(old, dict):
        add("loosened", "constraints dropped (now unbounded)")
    elif old != new:
        add("neutral", f"value changed ({old!r} -> {new!r})")

    direction = ("loosened" if state["loosened"]
                 else "tightened" if state["tightened"] else "neutral")
    return direction, deltas


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #

def _diff_grants(old, new, old_denied, new_denied):
    changes = []
    old_g, new_g = _grants(old), _grants(new)
    for key in sorted(set(old_g) | set(new_g)):
        sensitive = _sensitive_of([key])
        was_denied = _was_denied(key, *old_denied)
        if key not in old_g:
            changes.append(Change("grant", key, "grant added", "loosened",
                                  f"new grant ({_val_desc(new_g[key])})",
                                  sensitive, was_denied))
        elif key not in new_g:
            changes.append(Change("grant", key, "grant removed", "tightened",
                                  f"grant dropped (was {_val_desc(old_g[key])})",
                                  sensitive, was_denied))
        else:
            direction, deltas = _compare_value(old_g[key], new_g[key])
            if deltas:
                changes.append(Change("grant", key, "grant changed", direction,
                                      "; ".join(deltas), sensitive, was_denied))
    return changes


def _diff_deny(old, new):
    changes = []
    old_e, old_n = _deny_sets(old)
    new_e, new_n = _deny_sets(new)
    added = sorted((new_e | new_n) - (old_e | old_n))
    removed = sorted((old_e | old_n) - (new_e | new_n))
    for key in added:
        changes.append(Change("deny", f"deny:{key}", "deny added", "tightened",
                              "explicit refusal added", _sensitive_of([key]), False))
    for key in removed:
        changes.append(Change("deny", f"deny:{key}", "deny removed", "loosened",
                              "explicit refusal removed", _sensitive_of([key]), True))
    return changes


def _diff_expiry(old, new):
    old_ttl, new_ttl = old.get("ttl"), new.get("ttl")
    if old_ttl == new_ttl:
        return []
    if old_ttl is None:
        return [Change("expiry", "ttl", "expiry added", "tightened",
                       f"no ttl -> {new_ttl}", "", False)]
    if new_ttl is None:
        return [Change("expiry", "ttl", "expiry removed", "loosened",
                       f"{old_ttl} -> none (grants never lapse)", "", False)]
    old_d, new_d = _parse_iso_date(old_ttl), _parse_iso_date(new_ttl)
    if old_d is None or new_d is None:
        direction = "neutral"
        tail = ""
    elif new_d > old_d:
        direction = "loosened"
        tail = " (later)"
    elif new_d < old_d:
        direction = "tightened"
        tail = " (earlier)"
    else:
        return []
    return [Change("expiry", "ttl", "expiry changed", direction,
                   f"{old_ttl} -> {new_ttl}{tail}", "", False)]


def _diff_delegations(old, new, old_denied):
    changes = []
    old_d, new_d = _delegations(old), _delegations(new)
    for pair in sorted(set(old_d) | set(new_d), key=lambda p: (p[0], p[1])):
        frm, to = pair
        area = f"delegations:{frm}->{to}"
        if pair not in old_d:
            child = new_d[pair].get("narrower_than")
            keys = sorted(child) if isinstance(child, dict) else []
            changes.append(Change("delegation", area, "delegation added", "loosened",
                                  f"to {to} gains [{', '.join(keys)}]",
                                  _sensitive_of(keys),
                                  any(_was_denied(k, *old_denied) for k in keys)))
            continue
        if pair not in new_d:
            child = old_d[pair].get("narrower_than")
            keys = sorted(child) if isinstance(child, dict) else []
            changes.append(Change("delegation", area, "delegation removed", "tightened",
                                  f"to {to} removed (was [{', '.join(keys)}])",
                                  _sensitive_of(keys), False))
            continue

        old_child = old_d[pair].get("narrower_than")
        new_child = new_d[pair].get("narrower_than")
        old_child = old_child if isinstance(old_child, dict) else {}
        new_child = new_child if isinstance(new_child, dict) else {}
        deltas, touched = [], set()
        state = {"loosened": False, "tightened": False}
        for key in sorted(set(old_child) | set(new_child)):
            touched.add(key)
            if key not in old_child:
                deltas.append(f"{key}: delegated ({_val_desc(new_child[key])})")
                state["loosened"] = True
            elif key not in new_child:
                deltas.append(f"{key}: undelegated (was {_val_desc(old_child[key])})")
                state["tightened"] = True
            else:
                direction, parts = _compare_value(old_child[key], new_child[key])
                if parts:
                    deltas.append(f"{key}: " + "; ".join(parts))
                    if direction in state:
                        state[direction] = True
        if deltas:
            direction = ("loosened" if state["loosened"]
                         else "tightened" if state["tightened"] else "neutral")
            changes.append(Change("delegation", area, "delegation changed", direction,
                                  "; ".join(deltas), _sensitive_of(touched),
                                  any(_was_denied(k, *old_denied) for k in touched)))
    return changes


def _diff_audit(old, new):
    def receipts(manifest):
        audit = manifest.get("audit")
        if isinstance(audit, dict) and isinstance(audit.get("receipt_of"), list):
            return {str(r) for r in audit["receipt_of"]}
        return set()

    old_r, new_r = receipts(old), receipts(new)
    changes = []
    for key in sorted(old_r - new_r):
        changes.append(Change("audit", f"audit.receipt_of:{key}", "receipt dropped",
                              "loosened", "receipt requirement removed",
                              _sensitive_of([key]), False))
    for key in sorted(new_r - old_r):
        changes.append(Change("audit", f"audit.receipt_of:{key}", "receipt added",
                              "tightened", "receipt requirement added",
                              _sensitive_of([key]), False))
    return changes


def diff_manifests(old, new):
    """Compare two loaded manifests -> deterministically ordered ``Change``s."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise DiffError("both sides of a diff must be manifest mappings")
    old_denied, new_denied = _deny_sets(old), _deny_sets(new)
    changes = []
    changes.extend(_diff_grants(old, new, old_denied, new_denied))
    changes.extend(_diff_deny(old, new))
    changes.extend(_diff_expiry(old, new))
    changes.extend(_diff_delegations(old, new, old_denied))
    changes.extend(_diff_audit(old, new))
    changes.sort(key=lambda c: (GROUP_ORDER.index(c.group), c.area, c.kind))
    return changes


def diff_files(old_path, new_path):
    """Load two manifest files and diff them. Raises :class:`ManifestError`."""
    old = load_manifest_file(old_path)
    new = load_manifest_file(new_path)
    return diff_manifests(old, new)


# --------------------------------------------------------------------------- #
# Gate, summary, rendering
# --------------------------------------------------------------------------- #

def is_gated(change) -> bool:
    """True when this change trips the ``--strict`` loosening gate."""
    if change.direction != "loosened":
        return False
    return bool(change.sensitive) or change.was_denied


def gate_hits(changes):
    return [c for c in changes if is_gated(c)]


def summarize(changes) -> dict:
    def count(**want):
        return sum(1 for c in changes
                   if all(getattr(c, k) == v for k, v in want.items()))
    return {
        "grants_added": count(group="grant", kind="grant added"),
        "grants_removed": count(group="grant", kind="grant removed"),
        "grants_tightened": count(group="grant", direction="tightened"),
        "grants_loosened": count(group="grant", direction="loosened"),
        "deny_added": count(group="deny", kind="deny added"),
        "deny_removed": count(group="deny", kind="deny removed"),
        "expiry_changed": count(group="expiry"),
        "delegations_changed": count(group="delegation"),
        "audit_changed": count(group="audit"),
        "total": len(changes),
        "gate_hits": len(gate_hits(changes)),
    }


def render(old_label, new_label, changes) -> str:
    """Human table (deterministic — column widths derive from the content)."""
    lines = [f"== manifesto diff | {old_label} -> {new_label}"]
    if not changes:
        lines.append("  no changes")
    else:
        w_area = max([len("AREA")] + [len(c.area) for c in changes])
        w_kind = max([len("CHANGE")] + [len(c.kind) for c in changes])
        lines.append(f"{'AREA':<{w_area}}  {'CHANGE':<{w_kind}}  DETAIL")
        for c in changes:
            lines.append(f"{c.area:<{w_area}}  {c.kind:<{w_kind}}  {c.detail}")
    s = summarize(changes)
    lines.append(
        "== {grants_added} grant(s) added, {grants_removed} removed, "
        "{grants_tightened} tightened, {grants_loosened} loosened; "
        "{deny_added} deny added, {deny_removed} deny removed; "
        "{expiry_changed} expiry changed, {delegations_changed} delegation(s) "
        "changed, {audit_changed} audit changed; {total} change(s) total".format(**s))
    hits = gate_hits(changes)
    if hits:
        lines.append(f"!! gate: {len(hits)} loosening(s) in a sensitive class "
                     "(payments/fs/shell) or a previously-denied area:")
        for c in hits:
            lines.append(f"   - [{c.sensitive or 'previously-denied'}] "
                         f"{c.area}: {c.detail}")
    return "\n".join(lines)


def as_json(old_label, new_label, changes, *, allow_loosening=False) -> str:
    hits = gate_hits(changes)
    payload = {
        "old": old_label,
        "new": new_label,
        "changes": [c._asdict() for c in changes],
        "summary": summarize(changes),
        "gate": {
            "triggered": bool(hits),
            "hits": [c.area for c in hits],
            "allow_loosening": bool(allow_loosening),
            "strict_would_fail": bool(hits) and not allow_loosening,
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
