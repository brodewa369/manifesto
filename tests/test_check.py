"""``manifesto check`` — the pre-action gate decision function.

Covers: ALLOW / DENY / REQUIRES_APPROVAL verdicts, ttl expiry, deny
dominance, absent-grant default-deny, amount/rate cap violations, to_domains
restriction, approval gates (required and readonly-allowed), the org registry
risk-class gate, delegation tightened-slice evaluation, CLI exit-code
contract (0/1/2/3), and determinism.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto import cli  # noqa: E402
from manifesto.check import (ALLOW, DENY, EXIT, REQUIRES_APPROVAL,  # noqa: E402
                             CheckError, check)
from manifesto.schema import load_manifest_file, load_verb_registry  # noqa: E402

SALES = ROOT / "examples" / "sales-ops-bot.yaml"
LEAD = ROOT / "examples" / "delegating-lead.yaml"
REGISTRY = ROOT / "fixtures" / "registry" / "acme.yaml"
EXPIRED = ROOT / "fixtures" / "home" / "manifests" / "hermes.manifest.yaml"

NOW = date(2026, 1, 1)


def _load(path):
    return load_manifest_file(path)


class AllowDeny(unittest.TestCase):
    def test_allow_bare_grant(self):
        d = check(_load(SALES), "gmail.read", now=NOW)
        self.assertEqual(d.verdict, ALLOW)
        self.assertIn("grants.gmail.read", d.clause)

    def test_deny_absent_grant_default_deny(self):
        d = check(_load(SALES), "github.write", now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("not granted", d.reason)

    def test_deny_explicit_deny_entry(self):
        d = check(_load(SALES), "payments.payout", amount=10, now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("explicitly denied", d.reason)
        self.assertIn("deny[0]", d.clause)

    def test_deny_namespace_dominates(self):
        manifest = {"agent": "a", "grants": {"fs.read": True}, "deny": ["fs"]}
        d = check(manifest, "fs.read", now=NOW)
        self.assertEqual(d.verdict, DENY)

    def test_deny_expired_contract(self):
        d = check(_load(EXPIRED), "fs.read", now=date(2026, 1, 1))
        self.assertEqual(d.verdict, DENY)
        self.assertIn("expired", d.reason)


class Caps(unittest.TestCase):
    def test_amount_within_cap_allows(self):
        d = check(_load(SALES), "payments.charge", amount=100, now=NOW)
        # readonly-allowed + write verb -> approval, but NOT a cap violation
        self.assertEqual(d.verdict, REQUIRES_APPROVAL)
        self.assertIn("readonly-allowed", d.reason)

    def test_amount_over_cap_denies(self):
        d = check(_load(SALES), "payments.charge", amount=900, now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("exceeds the cap", d.reason)
        self.assertIn("amount.max", d.clause)

    def test_rate_over_cap_denies(self):
        manifest = {"agent": "a",
                    "grants": {"gmail.send": {"rate": {"max": 20, "window": "P1D"}}}}
        d = check(manifest, "gmail.send", rate=50, now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("rate", d.reason)

    def test_rate_within_cap_allows(self):
        manifest = {"agent": "a",
                    "grants": {"gmail.send": {"rate": {"max": 20, "window": "P1D"}}}}
        d = check(manifest, "gmail.send", rate=10, now=NOW)
        self.assertEqual(d.verdict, ALLOW)

    def test_to_domains_violation_denies(self):
        manifest = {"agent": "a",
                    "grants": {"http.post": {"to_domains": ["example.com"]}}}
        d = check(manifest, "http.post", target="evil.com", now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("to_domains", d.clause)

    def test_to_domains_match_allows(self):
        manifest = {"agent": "a",
                    "grants": {"http.post": {"to_domains": ["example.com"]}}}
        d = check(manifest, "http.post", target="example.com", now=NOW)
        self.assertEqual(d.verdict, ALLOW)


class Approval(unittest.TestCase):
    def test_approval_required_forces_approval(self):
        manifest = {"agent": "a", "grants": {"shell.exec": {"approval": "required"}}}
        d = check(manifest, "shell.exec", now=NOW)
        self.assertEqual(d.verdict, REQUIRES_APPROVAL)
        self.assertIn("approval = required", d.clause)

    def test_readonly_allowed_blocks_write_verb(self):
        d = check(_load(SALES), "payments.charge", amount=100, now=NOW)
        self.assertEqual(d.verdict, REQUIRES_APPROVAL)
        self.assertIn("write action", d.reason)

    def test_readonly_allowed_permits_read_verb(self):
        manifest = {"agent": "a",
                    "grants": {"gmail.read": {"approval": "readonly-allowed"}}}
        d = check(manifest, "gmail.read", now=NOW)
        self.assertEqual(d.verdict, ALLOW)


class RegistryGate(unittest.TestCase):
    def test_gated_risk_class_forces_approval(self):
        reg = load_verb_registry(REGISTRY)
        # gmail.send is 'critical' (gated) in the fixture registry
        d = check(_load(SALES), "gmail.send", registry=reg, now=NOW)
        self.assertEqual(d.verdict, REQUIRES_APPROVAL)
        self.assertIn("critical", d.reason)

    def test_ungated_risk_class_allows(self):
        reg = load_verb_registry(REGISTRY)
        # calendar.read is granted and carries no risk class -> unaffected
        d = check(_load(SALES), "calendar.read", registry=reg, now=NOW)
        self.assertEqual(d.verdict, ALLOW)


class Delegation(unittest.TestCase):
    def test_delegated_slice_allows_within_child_cap(self):
        d = check(_load(LEAD), "gmail.send", rate=10, as_agent="sales-ops", now=NOW)
        self.assertEqual(d.verdict, ALLOW)
        self.assertIn("delegations[0].narrower_than", d.clause)

    def test_delegated_slice_denies_over_child_cap(self):
        d = check(_load(LEAD), "payments.charge", amount=100,
                  as_agent="sales-ops", now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("50", d.reason)  # child cap, not parent's 500

    def test_unknown_delegation_denies(self):
        d = check(_load(LEAD), "gmail.send", as_agent="nobody", now=NOW)
        self.assertEqual(d.verdict, DENY)
        self.assertIn("no delegation", d.reason)


class Errors(unittest.TestCase):
    def test_bad_action_key_raises(self):
        with self.assertRaises(CheckError):
            check(_load(SALES), "not-a-key", now=NOW)

    def test_non_mapping_manifest_raises(self):
        with self.assertRaises(CheckError):
            check("not-a-mapping", "gmail.read", now=NOW)


class ExitCodesAndCli(unittest.TestCase):
    def test_exit_map(self):
        self.assertEqual(EXIT[ALLOW], 0)
        self.assertEqual(EXIT[DENY], 1)
        self.assertEqual(EXIT[REQUIRES_APPROVAL], 3)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_cli_allow_exit_0(self):
        code, out = self._run(["check", str(SALES), "--action", "gmail.read"])
        self.assertEqual(code, 0)
        self.assertIn("ALLOW", out)

    def test_cli_deny_exit_1(self):
        code, out = self._run(["check", str(SALES), "--action", "payments.payout",
                               "--amount", "10"])
        self.assertEqual(code, 1)
        self.assertIn("DENY", out)

    def test_cli_requires_approval_exit_3(self):
        code, out = self._run(["check", str(SALES), "--action", "shell.exec"])
        # sales-ops-bot denies shell.exec -> DENY (1), not approval
        self.assertEqual(code, 1)

    def test_cli_requires_approval_exit_3_direct(self):
        code, out = self._run(["check", str(LEAD), "--action", "shell.exec"])
        self.assertEqual(code, 3)
        self.assertIn("REQUIRES_APPROVAL", out)

    def test_cli_json(self):
        code, out = self._run(["check", str(SALES), "--action", "gmail.read",
                               "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "ALLOW")
        self.assertEqual(payload["exit_code"], 0)

    def test_cli_bad_action_exit_2(self):
        code, _ = self._run(["check", str(SALES), "--action", "nope"])
        self.assertEqual(code, 2)

    def test_determinism(self):
        m = _load(SALES)
        a = check(m, "payments.charge", amount=900, now=NOW)
        b = check(m, "payments.charge", amount=900, now=NOW)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
