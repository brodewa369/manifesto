"""Canary: validator behaviour is DERIVED FROM the published schema file.

These tests do not merely assert current behaviour — they *mutate the schema
document* and prove the validator follows. If the structural layer were a
hand-coded duplicate, mutating the schema would change nothing; because the
engine walks the file at call time, the behaviour flips with it.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.schema import load_schema, validate  # noqa: E402


def _write_schema(mutate):
    """Load the real schema, apply ``mutate(doc)``, write to a temp file."""
    doc = copy.deepcopy(load_schema())
    mutate(doc)
    fh = tempfile.NamedTemporaryFile("w", suffix=".schema.json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return Path(fh.name)


def _error_codes(manifest, schema_path=None):
    return {f.code for f in validate(manifest, schema_path_=schema_path)
            if f.severity == "error"}


BASE_NO_TTL = {"agent": "canary", "grants": {"gmail.read": True}}


class CanaryTtlRequired(unittest.TestCase):
    """Make `ttl` required in the schema -> validator must demand it."""

    def test_real_schema_does_not_require_ttl(self):
        doc = load_schema()
        self.assertNotIn("ttl", doc["required"])

    def test_baseline_no_ttl_is_only_a_warning(self):
        # missing ttl is a WARNING (missing_ttl), not an error, by default
        self.assertNotIn("missing_required", _error_codes(BASE_NO_TTL))
        self.assertNotIn("missing_required",
                         {f.code for f in validate(BASE_NO_TTL)})

    def test_mutating_schema_to_require_ttl_changes_behaviour(self):
        schema = _write_schema(lambda d: d["required"].append("ttl"))
        try:
            errs = _error_codes(BASE_NO_TTL, schema)
            self.assertIn("missing_required", errs,
                          "schema now requires ttl -> must become an error")
            ttl_findings = [f for f in validate(BASE_NO_TTL, schema_path_=schema)
                            if f.keypath == "ttl" and f.severity == "error"]
            self.assertTrue(ttl_findings)
        finally:
            schema.unlink(missing_ok=True)


class CanaryRequiredAgent(unittest.TestCase):
    """Drop `agent` from required -> a manifest without it must stop erroring."""

    def test_removing_required_agent_removes_the_error(self):
        no_agent = {"ttl": "2027-01-01", "grants": {"gmail.read": True}}
        self.assertIn("missing_agent", _error_codes(no_agent))  # default
        schema = _write_schema(lambda d: d["required"].remove("agent"))
        try:
            self.assertNotIn("missing_agent", _error_codes(no_agent, schema))
        finally:
            schema.unlink(missing_ok=True)


class CanaryEnumDerived(unittest.TestCase):
    """Widen the approval enum in the schema -> new value becomes legal."""

    SHELL = {"agent": "a", "grants": {"shell.exec": {"approval": "sometimes"}}}

    def test_default_enum_rejects_sometimes(self):
        self.assertIn("bad_approval", _error_codes(self.SHELL))

    def test_widened_enum_accepts_sometimes(self):
        def widen(d):
            appr = d["definitions"]["grantValue"]["properties"]["approval"]
            appr["enum"].append("sometimes")
        schema = _write_schema(widen)
        try:
            self.assertNotIn("bad_approval", _error_codes(self.SHELL, schema))
        finally:
            schema.unlink(missing_ok=True)


class CanaryKeyPattern(unittest.TestCase):
    """Relax the grant-key pattern in the schema -> ungroupable keys pass."""

    BAD = {"agent": "a", "grants": {"nodot": True}}

    def test_default_pattern_rejects_nodot(self):
        self.assertIn("ungroupable_verb", _error_codes(self.BAD))

    def test_relaxed_pattern_accepts_nodot(self):
        def relax(d):
            pn = d["definitions"]["grantsMap"]["propertyNames"]
            pn["pattern"] = "^[a-z][a-z0-9_.]*$"
        schema = _write_schema(relax)
        try:
            self.assertNotIn("ungroupable_verb", _error_codes(self.BAD, schema))
        finally:
            schema.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
