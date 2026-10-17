"""Org verb-registry override (``manifesto validate --registry``).

Covers: extend/replace modes, the three ``unknown_verb`` rules, the
risk-class → ``approval: required`` rule, strict parsing of an authority file,
and the CLI exit-code contract. `tests/test_validate.py` covers the built-in
registry with no override — those must be untouched by this feature.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto import cli  # noqa: E402
from manifesto.schema import (  # noqa: E402
    ManifestError, KNOWN, load_manifest_file, load_verb_registry,
    split_findings, validate,
)

REGISTRY = ROOT / "fixtures" / "registry" / "acme.yaml"
ORG_BOT = ROOT / "fixtures" / "registry" / "acme-deploy-bot.yaml"
SALES = ROOT / "examples" / "sales-ops-bot.yaml"

NOW = date(2026, 1, 1)


def _findings(manifest, registry=None):
    return validate(manifest, now=NOW, registry=registry)


def _by_code(manifest, registry=None):
    return {f.code: f for f in _findings(manifest, registry)}


def _errors(manifest, registry=None):
    return [f for f in _findings(manifest, registry) if f.severity == "error"]


class FixtureRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_verb_registry(REGISTRY)

    def test_fixture_shape(self):
        self.assertEqual(self.reg.label, "acme-corp")
        self.assertEqual(self.reg.mode, "extend")
        self.assertEqual(self.reg.unknown_verb, "warning")
        self.assertEqual(self.reg.unknown_severity, "warning")
        self.assertEqual(self.reg.systems["acme"],
                         ("deploy", "rollback", "status"))

    def test_extend_keeps_the_built_in_registry(self):
        known = self.reg.known()
        self.assertIn("gmail", known)
        self.assertEqual(known["gmail"], KNOWN["gmail"])
        self.assertIn("acme", known)
        self.assertIn("deploy", known["acme"])

    def test_risk_class_lookup(self):
        self.assertEqual(self.reg.risk_class("acme.deploy"), "critical")
        self.assertEqual(self.reg.gated_risk_class("acme.deploy"), "critical")
        self.assertIsNone(self.reg.gated_risk_class("acme.rollback"))  # high
        self.assertIsNone(self.reg.risk_class("gmail.read"))

    def test_describe_is_json_ready(self):
        payload = json.loads(json.dumps(self.reg.describe()))
        self.assertEqual(payload["label"], "acme-corp")
        self.assertEqual(payload["systems"], ["acme"])
        self.assertEqual(payload["risk_classes"], 5)
        self.assertEqual(payload["require_approval"], ["critical"])
        self.assertTrue(payload["source"].endswith("acme.yaml"))


class OrgVocabulary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_verb_registry(REGISTRY)
        cls.bot = load_manifest_file(ORG_BOT)

    def test_org_verbs_fail_without_the_override(self):
        codes = {f.code for f in _findings(self.bot)}
        self.assertIn("unknown_verb", codes)
        self.assertEqual(len(_errors(self.bot)), 3)   # 2 grants + 1 deny

    def test_org_verbs_validate_clean_with_the_override(self):
        findings = _findings(self.bot, self.reg)
        self.assertEqual(findings, [])

    def test_unknown_rule_warning_downgrades_not_removes(self):
        reg = self.reg
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.readd": True}}
        f = [x for x in _findings(m, reg) if x.code == "unknown_verb"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].severity, "warning")
        self.assertEqual(_errors(m, reg), [])
        # ... and --strict then fails it
        self.assertTrue(f[0].hint)

    def test_unknown_rule_error_is_the_default_without_a_registry(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.readd": True}}
        f = [x for x in _findings(m) if x.code == "unknown_verb"]
        self.assertEqual(f[0].severity, "error")

    def test_replace_mode_closes_the_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "closed.yaml"
            p.write_text("mode: replace\nsystems:\n  acme: [deploy]\n",
                         encoding="utf-8")
            reg = load_verb_registry(p)
            known = reg.known()
            self.assertNotIn("gmail", known)
            self.assertIn("acme", known)
            m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.read": True}}
            f = [x for x in _findings(m, reg) if x.code == "unknown_verb"]
            self.assertEqual(len(f), 1)
            self.assertIn("gmail", f[0].message)


class UnknownVerbRules(unittest.TestCase):
    def _registry(self, rule, tmp):
        p = Path(tmp) / f"{rule}.yaml"
        p.write_text(f"registry: t\nunknown_verb: {rule}\n", encoding="utf-8")
        return load_verb_registry(p)

    def test_three_rules(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.readd": True}}
        with tempfile.TemporaryDirectory() as tmp:
            error = [x for x in _findings(m, self._registry("error", tmp))
                     if x.code == "unknown_verb"]
            self.assertEqual([x.severity for x in error], ["error"])

            warn = [x for x in _findings(m, self._registry("warning", tmp))
                    if x.code == "unknown_verb"]
            self.assertEqual([x.severity for x in warn], ["warning"])

            ignored = [x for x in _findings(m, self._registry("ignore", tmp))
                       if x.code == "unknown_verb"]
            self.assertEqual(ignored, [])


class RiskClassRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_verb_registry(REGISTRY)   # gmail.send -> critical -> gated

    def test_bare_grant_needs_approval(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.send": True}}
        f = _by_code(m, self.reg)
        self.assertIn("missing_approval", f)
        self.assertEqual(f["missing_approval"].severity, "error")
        self.assertIn("critical", f["missing_approval"].message)
        self.assertIn("approval: required", f["missing_approval"].hint)

    def test_readonly_allowed_does_not_satisfy_the_gate(self):
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"gmail.send": {"approval": "readonly-allowed"}}}
        self.assertIn("missing_approval", _by_code(m, self.reg))

    def test_required_approval_satisfies_the_gate(self):
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"gmail.send": {"approval": "required",
                                       "rate": {"max": 10, "window": "P1D"}}}}
        self.assertNotIn("missing_approval", _by_code(m, self.reg))
        self.assertEqual(_errors(m, self.reg), [])

    def test_ungated_risk_class_needs_no_approval(self):
        # payments.charge is 'high' in the fixture registry; only 'critical' is gated
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"payments.charge": {"amount": {"max": 50, "currency": "USD",
                                                       "window": "P1D"}}}}
        self.assertNotIn("missing_approval", _by_code(m, self.reg))
        self.assertEqual(_errors(m, self.reg), [])

    def test_risk_rule_absent_without_a_registry(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.send": True}}
        self.assertNotIn("missing_approval", _by_code(m))

    def test_sales_ops_example_is_flagged_only_under_the_org_registry(self):
        m = load_manifest_file(SALES)
        self.assertNotIn("missing_approval", _by_code(m))
        self.assertIn("missing_approval", _by_code(m, self.reg))
        self.assertIn("missing_approval", {f.code for f in _errors(m, self.reg)})


class MalformedRegistry(unittest.TestCase):
    CASES = (
        ("registry: t\nbogus: 1\n", "unknown registry key"),
        ("mode: sideways\n", "`mode` must be one of"),
        ("unknown_verb: loud\n", "`unknown_verb` must be one of"),
        ("systems: not-a-map\n", "`systems` must map"),
        ("systems:\n  acme: deploy\n", "must list its verbs"),
        ("systems:\n  'ACME!': [deploy]\n", "must be one lowercase segment"),
        ("risk_classes:\n  gmail: high\n", "must be <system>.<verb>"),
        ("risk_classes:\n  gmail.send: 5\n", "non-empty string"),
        ("risk_classes:\n  gmail.sendit: high\n", "does not know"),
        ("risk_classes:\n  ghost.verb: high\n", "does not know"),
        ("require_approval: critical\n", "`require_approval` must be a list"),
        ("- this is a list, not a mapping\n", "must be a mapping"),
    )

    def test_bad_registries_raise_rather_than_silently_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i, (text, expect) in enumerate(self.CASES):
                with self.subTest(case=expect):
                    p = Path(tmp) / f"bad{i}.yaml"
                    p.write_text(text, encoding="utf-8")
                    with self.assertRaises(ManifestError) as ctx:
                        load_verb_registry(p)
                    self.assertIn(expect, str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(ManifestError):
            load_verb_registry(ROOT / "fixtures" / "registry" / "nope.yaml")


class CliRegistry(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_org_manifest_without_registry_exits_1(self):
        rc, out = self._run(["validate", str(ORG_BOT)])
        self.assertEqual(rc, 1)
        self.assertIn("unknown system 'acme'", out)
        self.assertNotIn("registry:", out)

    def test_org_manifest_with_registry_exits_0(self):
        rc, out = self._run(["validate", str(ORG_BOT), "--registry", str(REGISTRY)])
        self.assertEqual(rc, 0)
        self.assertIn("registry: acme-corp (mode: extend, unknown_verb: warning)", out)
        self.assertIn("no findings", out)

    def test_risk_class_violation_exits_1(self):
        rc, out = self._run(["validate", str(SALES), "--registry", str(REGISTRY)])
        self.assertEqual(rc, 1)
        self.assertIn("risk class 'critical'", out)
        self.assertIn("approval: required", out)

    def test_missing_registry_file_exits_2(self):
        rc, _ = self._run(["validate", str(SALES), "--registry", "no/such.yaml"])
        self.assertEqual(rc, 2)

    def test_malformed_registry_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.yaml"
            p.write_text("mode: sideways\n", encoding="utf-8")
            rc, _ = self._run(["validate", str(SALES), "--registry", str(p)])
            self.assertEqual(rc, 2)

    def test_json_carries_the_registry_block(self):
        rc, out = self._run(["validate", str(ORG_BOT), "--registry",
                             str(REGISTRY), "--json"])
        payload = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["registry"]["label"], "acme-corp")
        self.assertEqual(payload["registry"]["unknown_verb"], "warning")

    def test_json_without_registry_has_no_registry_block(self):
        rc, out = self._run(["validate", str(SALES), "--json"])
        payload = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertNotIn("registry", payload)

    def test_registry_warning_becomes_strict_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = Path(tmp) / "draft.yaml"
            m.write_text("agent: a\nttl: 2027-01-01\ngrants:\n  gmail.readd: true\n",
                         encoding="utf-8")
            rc, _ = self._run(["validate", str(m), "--registry", str(REGISTRY)])
            self.assertEqual(rc, 0)
            rc, _ = self._run(["validate", str(m), "--registry", str(REGISTRY),
                               "--strict"])
            self.assertEqual(rc, 1)


class NoRegressionWithoutRegistry(unittest.TestCase):
    def test_every_example_still_validates_clean(self):
        for name in ("sales-ops-bot", "coding-agent", "personal-assistant",
                     "delegating-lead"):
            with self.subTest(name=name):
                m = load_manifest_file(ROOT / "examples" / f"{name}.yaml")
                errors, _ = split_findings(validate(m))
                self.assertEqual(errors, [])

    def test_validate_signature_default_is_unchanged(self):
        m = load_manifest_file(SALES)
        self.assertEqual(validate(m), validate(m, registry=None))


if __name__ == "__main__":
    unittest.main()
