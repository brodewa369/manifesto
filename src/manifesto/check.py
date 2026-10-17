"""``manifesto check`` — the pre-action gate (a pure decision function).

A contract with no runtime teeth is a document. ``manifesto check`` is the
deterministic, embeddable decision function that gives a manifest real-time
authority **without** a runtime proxy: hand it the *current* manifest and a
*proposed* action, and it answers ``ALLOW`` / ``DENY`` / ``REQUIRES_APPROVAL``
with a human reason and the **exact matching clause** that decided it.

It is pure standard library, side-effect free, and deterministic: the same
manifest, action and clock always yield the same decision. An integrator can
call :func:`check` directly (it is the contract's semantics, exposed) or shell
out to ``manifesto check`` (exit codes below).

What it evaluates, in this precedence (first decisive clause wins):

1. **Expiry** — a manifest (or the selected delegation) whose ``ttl`` has
   passed denies everything: the contract has lapsed.
2. **Deny** — an explicit ``deny:`` entry (exact key or whole-namespace)
   dominates, so it denies before any grant is consulted.
3. **Grant presence** — an action with no matching grant is denied (default
   deny); the reason names the absent clause.
4. **Constraint violations** — an ``amount``/``rate`` cap or a ``to_domains``
   restriction that the proposed action breaks denies, citing the bound.
5. **Approval gates** — a grant gated by ``approval: required``, a
   ``readonly-allowed`` grant asked to do a *write*, or an **org registry risk
   class** in ``require_approval`` all yield ``REQUIRES_APPROVAL``.
6. Otherwise the action is ``ALLOW``.

``--as <sub-agent>`` evaluates the action against a **delegation's tightened
slice** (its ``narrower_than`` set plus its own ttl), so a child is judged on
what it was actually handed — never on the parent's full power.

Exit codes (integrator-friendly, distinct from validate's 0/1/2):

* ``0`` — ALLOW
* ``1`` — DENY
* ``2`` — usage error / unreadable manifest or registry
* ``3`` — REQUIRES_APPROVAL (proceed only with a human sign-off)
"""
from __future__ import annotations

import json
from collections import namedtuple
from datetime import date

from .schema import (APPROVAL_STRICTNESS, ManifestError, NAMESPACE_RE,
                     _parse_iso_date, is_narrower, load_manifest_file)

#: Verdicts. ``ALLOW`` may proceed; ``DENY`` must not; ``REQUIRES_APPROVAL``
#: may proceed only after a human approves.
ALLOW = "ALLOW"
DENY = "DENY"
REQUIRES_APPROVAL = "REQUIRES_APPROVAL"

#: Exit code per verdict (``manifesto check``).
EXIT = {ALLOW: 0, DENY: 1, REQUIRES_APPROVAL: 3}

#: Verbs that read rather than mutate — a ``readonly-allowed`` grant lets
#: these through without a human, and blocks everything else.
READ_VERBS = {"read", "list", "search", "get", "status", "view"}

#: One decision. ``clause`` is the exact key-path that decided the verdict
#: (or ``""`` when nothing matched, e.g. an ungranted action).
Decision = namedtuple("Decision",
                      ["agent", "action", "verdict", "reason", "clause"])


class CheckError(ManifestError):
    """Raised when a proposed action spec cannot be evaluated at all."""


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _verb(action: str) -> str:
    return str(action).partition(".")[2]


def _namespace_covered(action: str, deny) -> bool:
    """True when a ``deny`` entry covers ``action`` (exact or whole-system)."""
    system = str(action).partition(".")[0]
    for entry in deny or ():
        if not isinstance(entry, str):
            continue
        if entry == action or (entry == system and "." not in entry):
            return True
    return False


def _delegation_for(manifest, as_agent):
    """Return ``(index, delegation)`` for ``as_agent``, or ``(None, None)``."""
    delegations = manifest.get("delegations")
    if not isinstance(delegations, list):
        return None, None
    for i, d in enumerate(delegations):
        if isinstance(d, dict) and d.get("to") == as_agent:
            return i, d
    return None, None


def check(manifest, action, *, amount=None, rate=None, target=None,
          as_agent=None, registry=None, now=None) -> Decision:
    """Decide a proposed ``action`` against ``manifest``. Pure + deterministic.

    ``manifest`` — a loaded manifest mapping.
    ``action``   — the proposed ``<system>.<verb>`` (e.g. ``payments.charge``).
    ``amount``   — proposed money amount (numeric), checked against ``amount.max``.
    ``rate``     — proposed call count, checked against ``rate.max``.
    ``target``   — proposed outbound target, checked against ``to_domains``.
    ``as_agent`` — evaluate as this delegated sub-agent (uses its tightened
                   ``narrower_than`` slice and delegation ttl).
    ``registry`` — an org :class:`~manifesto.schema.VerbRegistry` whose gated
                   risk classes force ``REQUIRES_APPROVAL``.
    ``now``      — clock override (defaults to ``date.today()``) for tests.

    Returns a :class:`Decision` — never raises for a normal deny/allow path.
    """
    now = now or date.today()
    if not isinstance(manifest, dict):
        raise CheckError("manifest must be a mapping")
    if not isinstance(action, str) or not NAMESPACE_RE.match(action):
        raise CheckError(
            f"action {action!r} must be a <system>.<verb> key, e.g. payments.charge")

    agent = manifest.get("agent", "(unnamed)")

    # ---- 1. expiry: the contract (or the delegation) has lapsed ------------ #
    ttl_raw = manifest.get("ttl")
    ttl_date = _parse_iso_date(ttl_raw) if isinstance(ttl_raw, str) else None
    if ttl_date is not None and ttl_date < now:
        return Decision(agent, action, DENY,
                        f"contract expired on {ttl_date.isoformat()} — all grants lapsed",
                        f"ttl = {ttl_raw}")

    # ---- resolve the effective grant scope (parent, or a delegation) ------ #
    if as_agent is not None:
        idx, d = _delegation_for(manifest, as_agent)
        if d is None:
            return Decision(agent, action, DENY,
                            f"no delegation to '{as_agent}' in this contract",
                            "delegations")
        d_ttl = d.get("ttl")
        d_date = _parse_iso_date(d_ttl) if isinstance(d_ttl, str) else None
        if d_date is not None and d_date < now:
            return Decision(agent, action, DENY,
                            f"delegation to '{as_agent}' expired on {d_date.isoformat()}",
                            f"delegations[{idx}].ttl = {d_ttl}")
        grants = d.get("narrower_than") if isinstance(d.get("narrower_than"), dict) else {}
        scope = f"delegations[{idx}].narrower_than"
        agent = as_agent
    else:
        grants = manifest.get("grants") if isinstance(manifest.get("grants"), dict) else {}
        scope = "grants"

    # ---- 2. deny dominates (checked against the parent's deny list) ------- #
    deny = manifest.get("deny") if isinstance(manifest.get("deny"), list) else []
    if _namespace_covered(action, deny):
        for i, entry in enumerate(deny):
            if entry == action or (entry == str(action).partition(".")[0]
                                   and "." not in str(entry)):
                return Decision(agent, action, DENY,
                                f"'{action}' is explicitly denied — deny dominates any grant",
                                f"deny[{i}] = {entry}")

    # ---- 3. grant presence (default deny) ---------------------------------- #
    if action not in grants:
        return Decision(agent, action, DENY,
                        f"'{action}' is not granted — anything not granted is denied",
                        f"{scope}.{action} (absent)")

    value = grants[action]
    clause_base = f"{scope}.{action}"

    # ---- 4. constraint violations ------------------------------------------ #
    if isinstance(value, dict):
        amt = value.get("amount")
        if isinstance(amt, dict) and _is_number(amt.get("max")):
            cap = amt["max"]
            if amount is None:
                return Decision(agent, action, REQUIRES_APPROVAL,
                                f"'{action}' is capped at {cap}; no amount given, so "
                                "the cap cannot be verified",
                                f"{clause_base}.amount.max = {cap}")
            if not _is_number(amount):
                return Decision(agent, action, DENY,
                                f"proposed amount {amount!r} is not a number",
                                f"{clause_base}.amount.max = {cap}")
            if amount > cap:
                return Decision(agent, action, DENY,
                                f"amount {amount} exceeds the cap of {cap}",
                                f"{clause_base}.amount.max = {cap}")

        rate_blk = value.get("rate")
        if isinstance(rate_blk, dict) and _is_number(rate_blk.get("max")):
            cap = rate_blk["max"]
            if rate is None:
                return Decision(agent, action, REQUIRES_APPROVAL,
                                f"'{action}' is rate-capped at {cap}; no rate given, "
                                "so the cap cannot be verified",
                                f"{clause_base}.rate.max = {cap}")
            if _is_number(rate) and rate > cap:
                return Decision(agent, action, DENY,
                                f"rate {rate} exceeds the cap of {cap}",
                                f"{clause_base}.rate.max = {cap}")

        doms = value.get("to_domains")
        if isinstance(doms, list) and doms:
            allowed = {str(x) for x in doms}
            if target is None:
                return Decision(agent, action, REQUIRES_APPROVAL,
                                f"'{action}' is restricted to {len(allowed)} domain(s); "
                                "no target given, so the restriction cannot be verified",
                                f"{clause_base}.to_domains = [{', '.join(sorted(allowed))}]")
            if str(target) not in allowed:
                return Decision(agent, action, DENY,
                                f"target '{target}' is outside to_domains",
                                f"{clause_base}.to_domains = [{', '.join(sorted(allowed))}]")

        # ---- 5. approval gates --------------------------------------------- #
        approval = value.get("approval")
        if approval == "required":
            return Decision(agent, action, REQUIRES_APPROVAL,
                            f"'{action}' is gated by approval: required",
                            f"{clause_base}.approval = required")
        if approval == "readonly-allowed" and _verb(action) not in READ_VERBS:
            return Decision(agent, action, REQUIRES_APPROVAL,
                            f"'{action}' is readonly-allowed but '{_verb(action)}' is a "
                            "write action — a human must approve",
                            f"{clause_base}.approval = readonly-allowed")

    # ---- org registry risk-class gate -------------------------------------- #
    if registry is not None:
        cls = registry.gated_risk_class(action)
        if cls is not None:
            return Decision(agent, action, REQUIRES_APPROVAL,
                            f"'{action}' is risk class '{cls}' which the org registry "
                            "gates behind human approval",
                            f"registry risk class '{cls}' (require_approval)")

    # ---- 6. nothing denied it -> ALLOW ------------------------------------- #
    if value is True:
        clause = f"{clause_base} = true"
    else:
        clause = clause_base
    return Decision(agent, action, ALLOW,
                    f"'{action}' is granted within its stated bounds", clause)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render(manifest_label: str, decision: Decision) -> str:
    """Human one-shot report for a decision."""
    lines = [f"== manifesto check | {manifest_label}"]
    lines.append(f"   agent:  {decision.agent}")
    lines.append(f"   action: {decision.action}")
    lines.append(f"   => {decision.verdict}")
    lines.append(f"   reason: {decision.reason}")
    lines.append(f"   clause: {decision.clause}")
    return "\n".join(lines)


def as_json(manifest_label: str, decision: Decision) -> str:
    payload = {
        "manifest": manifest_label,
        "agent": decision.agent,
        "action": decision.action,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "clause": decision.clause,
        "exit_code": EXIT[decision.verdict],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def check_file(manifest_path, action, **kwargs) -> Decision:
    """Load a manifest file and :func:`check` one action against it."""
    manifest = load_manifest_file(manifest_path)
    return check(manifest, action, **kwargs)


__all__ = ["ALLOW", "DENY", "REQUIRES_APPROVAL", "EXIT", "READ_VERBS",
           "Decision", "CheckError", "check", "check_file", "render", "as_json",
           "APPROVAL_STRICTNESS", "is_narrower"]
