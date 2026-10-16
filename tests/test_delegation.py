"""Delegation attenuation tests (RFC-0001 § Delegation).

The validator must enforce that a delegation may only NARROW power:
``child.grants ⊆ parent.grants`` per-verb, with every parent-imposed
constraint carried at least as strictly. The centrepiece is the tricky case —
a child trying to take a verb the parent holds *only via a constraint* as a
bare ``true`` (unbounded) — which is escalation and must be rejected.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.explain import explain  # noqa: E402
from manifesto.export import export_agentward, export_denylist  # noqa: E402
from manifesto.schema import (load_manifest_file, split_findings,  # noqa: E402
                              validate)

NOW = date(2026, 1, 1)
EXAMPLE = ROOT / "examples" / "delegating-lead.yaml"


def _codes(manifest):
    return {f.code for f in validate(manifest, now=NOW)}


def _delegation_codes(manifest):
    return {f.code for f in validate(manifest, now=NOW)
            if f.code.startswith("delegation") or f.code == "unknown_parent"}


def _manifest(parent_grants, child_grants, *, frm="lead", to="ops",
              purpose="handoff", agent="lead"):
    delegation = {"from": frm, "to": to, "narrower_than": child_grants}
    if purpose is not None:
        delegation["purpose"] = purpose
    return {"agent": agent, "ttl": "2027-01-01",
            "grants": parent_grants, "delegations": [delegation]}


class ValidAttenuation(unittest.TestCase):
    def test_example_delegation_is_clean(self):
        m = load_manifest_file(EXAMPLE)
        errs, warns = split_findings(validate(m, now=NOW))
        self.assertEqual(errs, [])
        self.assertEqual(warns, [])

    def test_tighter_cap_is_valid(self):
        m = _manifest(
            {"payments.charge": {"amount": {"max": 500, "currency": "USD", "window": "P1D"}}},
            {"payments.charge": {"amount": {"max": 50, "currency": "USD", "window": "P1D"}}})
        self.assertNotIn("delegation_escalation", _codes(m))

    def test_equal_cap_is_valid(self):
        m = _manifest(
            {"payments.charge": {"amount": {"max": 500, "currency": "USD", "window": "P1D"}}},
            {"payments.charge": {"amount": {"max": 500, "currency": "USD", "window": "P1D"}}})
        self.assertNotIn("delegation_escalation", _codes(m))

    def test_child_may_add_extra_constraint(self):
        # parent has bare gmail.read; child additionally rate-limits it
        m = _manifest({"gmail.read": True},
                      {"gmail.read": {"rate": {"max": 5, "window": "P1D"}}})
        self.assertNotIn("delegation_escalation", _codes(m))

    def test_to_domains_subset_is_valid(self):
        m = _manifest({"http.post": {"to_domains": ["a.example", "b.example"]}},
                      {"http.post": {"to_domains": ["a.example"]}})
        self.assertNotIn("delegation_escalation", _codes(m))


class EscalationRejections(unittest.TestCase):
    def test_tricky_child_takes_constrained_verb_unbounded(self):
        # parent holds payments.charge ONLY via a cap; child wants it as `true`
        m = _manifest(
            {"payments.charge": {"amount": {"max": 500, "currency": "USD", "window": "P1D"}}},
            {"payments.charge": True})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_tricky_rated_verb_taken_unbounded(self):
        # parent holds gmail.send only as a rate-limited object
        m = _manifest({"gmail.send": {"rate": {"max": 100, "window": "P1D"}}},
                      {"gmail.send": True})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_looser_cap_is_escalation(self):
        m = _manifest(
            {"payments.charge": {"amount": {"max": 50, "currency": "USD", "window": "P1D"}}},
            {"payments.charge": {"amount": {"max": 500, "currency": "USD", "window": "P1D"}}})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_dropped_approval_gate_is_escalation(self):
        m = _manifest({"shell.exec": {"approval": "required"}},
                      {"shell.exec": {}})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_weakened_approval_is_escalation(self):
        m = _manifest({"shell.exec": {"approval": "required"}},
                      {"shell.exec": {"approval": "readonly-allowed"}})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_to_domains_superset_is_escalation(self):
        m = _manifest({"http.post": {"to_domains": ["a.example"]}},
                      {"http.post": {"to_domains": ["a.example", "evil.example"]}})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_granting_a_verb_parent_lacks_is_escalation(self):
        m = _manifest({"gmail.read": True}, {"gmail.send": True})
        self.assertIn("delegation_escalation", _delegation_codes(m))

    def test_dropped_rate_limit_is_escalation(self):
        m = _manifest({"gmail.send": {"rate": {"max": 10, "window": "P1D"}}},
                      {"gmail.send": {}})
        self.assertIn("delegation_escalation", _delegation_codes(m))


class DelegationStructure(unittest.TestCase):
    def test_missing_purpose_is_error(self):
        m = _manifest({"gmail.read": True}, {"gmail.read": True}, purpose=None)
        self.assertIn("delegation_missing_purpose", _codes(m))

    def test_unresolvable_parent_is_error(self):
        m = _manifest({"gmail.read": True}, {"gmail.read": True},
                      frm="not-the-agent", agent="lead")
        self.assertIn("unknown_parent", _delegation_codes(m))

    def test_delegation_object_requires_from_to(self):
        m = {"agent": "lead", "ttl": "2027-01-01", "grants": {"gmail.read": True},
             "delegations": [{"purpose": "handoff"}]}
        codes = _codes(m)
        self.assertTrue({"missing_required"} & codes)


class ExplainChain(unittest.TestCase):
    def test_explain_renders_attenuation_chain(self):
        m = load_manifest_file(EXAMPLE)
        text = explain(m, now=NOW)
        self.assertIn("delegations:", text)
        self.assertIn("sales-lead -> sales-ops", text)
        self.assertIn("purpose: coverage handoff", text)
        # narrowed bounds appear inline
        self.assertIn("at most 20", text)
        self.assertIn("up to 50 USD", text)


class DelegationExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_manifest_file(EXAMPLE)

    def test_agentward_includes_delegations_deterministically(self):
        a = export_agentward(self.m)
        self.assertEqual(a, export_agentward(self.m))
        self.assertIn("delegations:", a)
        self.assertIn("from: sales-lead", a)
        self.assertIn("to: sales-ops", a)
        self.assertIn("purpose: coverage handoff", a)
        self.assertIn("narrowedActions: [gmail.send, payments.charge]", a)

    def test_denylist_includes_delegations_deterministically(self):
        import json
        a = export_denylist(self.m)
        self.assertEqual(a, export_denylist(self.m))
        payload = json.loads(a)
        self.assertEqual(len(payload["delegations"]), 1)
        d = payload["delegations"][0]
        self.assertEqual(d["from"], "sales-lead")
        self.assertEqual(d["to"], "sales-ops")
        self.assertEqual(d["purpose"], "coverage handoff")
        self.assertIn("gmail.send", d["narrower_than"])


if __name__ == "__main__":
    unittest.main()
