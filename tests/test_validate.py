"""Validator matrix: one manifest per failure class, plus the happy path.

Zero third-party deps — plain ``unittest``. Each case is a dict literal so the
expected finding is obvious from the name.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.schema import (  # noqa: E402
    ManifestError, load_manifest_file, load_yaml, split_findings, validate,
)

NOW = date(2026, 1, 1)


def _codes(manifest):
    return {f.code for f in validate(manifest, now=NOW)}


def _errors(manifest):
    return [f for f in validate(manifest, now=NOW) if f.severity == "error"]


VALID = {
    "agent": "ok-bot",
    "ttl": "2027-01-01",
    "grants": {"gmail.read": True,
               "payments.charge": {"amount": {"max": 50, "currency": "USD", "window": "P1D"}},
               "shell.exec": {"approval": "required"}},
    "deny": ["fs.delete"],
}


class HappyPath(unittest.TestCase):
    def test_valid_manifest_has_no_findings(self):
        self.assertEqual(validate(VALID, now=NOW), [])

    def test_examples_validate_clean(self):
        for name in ("sales-ops-bot", "coding-agent", "personal-assistant",
                     "delegating-lead"):
            m = load_manifest_file(ROOT / "examples" / f"{name}.yaml")
            errors, _ = split_findings(validate(m))
            self.assertEqual(errors, [], f"{name}: {errors}")


class FailureMatrix(unittest.TestCase):
    def test_unknown_top_level_key(self):
        m = dict(VALID, bogus=1)
        self.assertIn("unknown_key", _codes(m))

    def test_unknown_verb_suggests(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.readd": True}}
        errs = _errors(m)
        self.assertTrue(any(f.code == "unknown_verb" for f in errs))
        self.assertTrue(any("read" in f.hint for f in errs), errs)

    def test_ungroupable_verb_no_dot(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmailread": True}}
        self.assertIn("ungroupable_verb", _codes(m))

    def test_ungroupable_verb_too_many_dots(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"a.b.c": True}}
        self.assertIn("ungroupable_verb", _codes(m))

    def test_unbounded_payment(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"payments.charge": True}}
        self.assertIn("unbounded_grant", _codes(m))

    def test_unbounded_shell(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"shell.exec": True}}
        self.assertIn("unbounded_grant", _codes(m))

    def test_empty_grant_object(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.read": {}}}
        self.assertIn("empty_grant", _codes(m))

    def test_shadowed_by_parent_deny(self):
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"gmail.send": True}, "deny": ["gmail"]}
        self.assertIn("shadowed_grant", _codes(m))

    def test_shadowed_by_exact_deny(self):
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"gmail.send": True}, "deny": ["gmail.send"]}
        self.assertIn("shadowed_grant", _codes(m))

    def test_expired_ttl_is_warning(self):
        m = {"agent": "a", "ttl": "2020-01-01", "grants": {"gmail.read": True}}
        f = validate(m, now=NOW)
        self.assertTrue(any(x.code == "expired" and x.severity == "warning" for x in f))

    def test_missing_ttl_is_warning(self):
        m = {"agent": "a", "grants": {"gmail.read": True}}
        self.assertIn("missing_ttl", _codes(m))

    def test_missing_agent_is_error(self):
        m = {"ttl": "2027-01-01", "grants": {"gmail.read": True}}
        self.assertIn("missing_agent", _codes(m))

    def test_bad_approval_value(self):
        m = {"agent": "a", "ttl": "2027-01-01",
             "grants": {"shell.exec": {"approval": "maybe"}}}
        self.assertIn("bad_approval", _codes(m))

    def test_audit_unknown_grant_warns(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.read": True},
             "audit": {"receipt_of": ["payments.charge"]}}
        self.assertIn("audit_unknown_grant", _codes(m))

    def test_findings_carry_keypath_and_hint(self):
        m = {"agent": "a", "ttl": "2027-01-01", "grants": {"payments.charge": True}}
        f = [x for x in validate(m, now=NOW) if x.code == "unbounded_grant"][0]
        self.assertEqual(f.keypath, "grants.payments.charge")
        self.assertTrue(f.hint)


class MiniYaml(unittest.TestCase):
    def test_scalars_and_nesting(self):
        doc = load_yaml(
            "agent: x\n"
            "ttl: 2027-01-01\n"
            "grants:\n"
            "  gmail.read: true\n"
            "  payments.charge:\n"
            "    amount:\n"
            "      max: 500\n"
            "deny: [shell.exec, fs.delete]\n"
        )
        self.assertEqual(doc["agent"], "x")
        self.assertIs(doc["grants"]["gmail.read"], True)
        self.assertEqual(doc["grants"]["payments.charge"]["amount"]["max"], 500)
        self.assertEqual(doc["deny"], ["shell.exec", "fs.delete"])

    def test_comments_and_quotes(self):
        doc = load_yaml("# comment\nkey: 'value # not a comment'  # real comment\n")
        self.assertEqual(doc["key"], "value # not a comment")


class Loader(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(ManifestError):
            load_manifest_file(ROOT / "examples" / "does-not-exist.yaml")

    def test_non_mapping_raises(self):
        p = ROOT / "examples" / "personal-assistant.yaml"
        m = load_manifest_file(p)
        self.assertIsInstance(m, dict)


if __name__ == "__main__":
    unittest.main()
