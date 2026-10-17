"""``manifesto diff`` — classification, the loosening gate, and exit codes.

Covers the four report classes the feature promises (tightened / loosened /
expiry / tie-cases), the ``--strict`` gate on payments/fs/shell plus
previously-denied areas, ``--allow-loosening``, JSON output, and determinism.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto import cli  # noqa: E402
from manifesto.diff import (  # noqa: E402
    DiffError, as_json, diff_files, diff_manifests, gate_hits, is_gated,
    render, summarize,
)
from manifesto.schema import load_manifest_file, split_findings, validate  # noqa: E402

DIFF_FIXTURES = ROOT / "fixtures" / "diff"
BEFORE = DIFF_FIXTURES / "before.yaml"
TIGHTENED = DIFF_FIXTURES / "after-tightened.yaml"
LOOSENED = DIFF_FIXTURES / "after-loosened.yaml"


def _base(**over):
    m = {"agent": "a", "ttl": "2027-01-01", "grants": {"gmail.read": True}}
    m.update(over)
    return m


def _changes(old, new):
    return diff_manifests(old, new)


def _by_area(changes):
    return {c.area: c for c in changes}


class Classification(unittest.TestCase):
    def test_no_change_is_no_change(self):
        self.assertEqual(_changes(_base(), _base()), [])

    def test_tie_case_equal_caps_is_not_a_change(self):
        old = _base(grants={"payments.charge": {"amount": {"max": 500}}})
        new = _base(grants={"payments.charge": {"amount": {"max": 500}}})
        self.assertEqual(_changes(old, new), [])

    def test_tie_case_identical_fixtures_produce_no_changes(self):
        self.assertEqual(diff_files(BEFORE, BEFORE), [])

    def test_lower_cap_is_tightened(self):
        old = _base(grants={"payments.charge": {"amount": {"max": 500}}})
        new = _base(grants={"payments.charge": {"amount": {"max": 100}}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "tightened")
        self.assertIn("amount.max 500 -> 100", c.detail)
        self.assertEqual(c.sensitive, "payments")

    def test_raised_cap_is_loosened(self):
        old = _base(grants={"payments.charge": {"amount": {"max": 500}}})
        new = _base(grants={"payments.charge": {"amount": {"max": 5000}}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "loosened")
        self.assertIn("amount.max 500 -> 5000", c.detail)

    def test_dropped_constraints_is_loosened(self):
        old = _base(grants={"payments.charge": {"amount": {"max": 500}}})
        new = _base(grants={"payments.charge": True})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "loosened")
        self.assertIn("now unbounded", c.detail)

    def test_added_constraints_is_tightened(self):
        old = _base(grants={"shell.exec": True})
        new = _base(grants={"shell.exec": {"approval": "required"}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "tightened")
        self.assertIn("constraints added", c.detail)

    def test_weakened_approval_is_loosened(self):
        old = _base(grants={"shell.exec": {"approval": "required"}})
        new = _base(grants={"shell.exec": {"approval": "readonly-allowed"}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "loosened")
        self.assertIn("approval required -> readonly-allowed", c.detail)

    def test_widened_domains_is_loosened_narrowed_is_tightened(self):
        old = _base(grants={"http.post": {"to_domains": ["a.example"]}})
        wide = _base(grants={"http.post": {"to_domains": ["a.example", "b.example"]}})
        narrow = _base(grants={"http.post": {"to_domains": []}})
        self.assertEqual(_changes(old, wide)[0].direction, "loosened")
        # an emptied domain list is dropped -> unrestricted -> loosened
        self.assertEqual(_changes(old, narrow)[0].direction, "loosened")
        shrunk = _base(grants={"http.post": {"to_domains": ["a.example", "c.example"]}})
        self.assertEqual(_changes(shrunk, old)[0].direction, "tightened")

    def test_mixed_change_is_loosened_worst_case(self):
        # rate tightened but the money cap raised -> overall loosening
        old = _base(grants={"payments.charge": {"amount": {"max": 500},
                                                "rate": {"max": 100}}})
        new = _base(grants={"payments.charge": {"amount": {"max": 5000},
                                                "rate": {"max": 10}}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "loosened")
        self.assertIn("rate.max 100 -> 10", c.detail)
        self.assertIn("amount.max 500 -> 5000", c.detail)

    def test_added_and_removed_grants(self):
        old = _base(grants={"gmail.read": True})
        new = _base(grants={"gmail.write": True})
        by = _by_area(_changes(old, new))
        self.assertEqual(by["gmail.write"].kind, "grant added")
        self.assertEqual(by["gmail.write"].direction, "loosened")
        self.assertEqual(by["gmail.read"].kind, "grant removed")
        self.assertEqual(by["gmail.read"].direction, "tightened")

    def test_deny_added_tightens_deny_removed_loosens(self):
        old = _base(deny=["fs.delete"])
        new = _base()
        by = _by_area(_changes(old, new))
        self.assertEqual(by["deny:fs.delete"].kind, "deny removed")
        self.assertEqual(by["deny:fs.delete"].direction, "loosened")
        self.assertTrue(by["deny:fs.delete"].was_denied)
        self.assertEqual(by["deny:fs.delete"].sensitive, "fs")
        added = _by_area(_changes(new, old))["deny:fs.delete"]
        self.assertEqual(added.kind, "deny added")
        self.assertEqual(added.direction, "tightened")

    def test_grant_previously_denied_is_marked(self):
        old = _base(grants={"gmail.read": True}, deny=["gmail.send"])
        new = _base(grants={"gmail.read": True, "gmail.send": True})
        (c,) = [x for x in _changes(old, new) if x.area == "gmail.send"]
        self.assertEqual(c.kind, "grant added")
        self.assertTrue(c.was_denied)
        self.assertTrue(is_gated(c))          # previously-denied, non-sensitive
        self.assertEqual(c.sensitive, "")      # gmail is not a sensitive class


class Expiry(unittest.TestCase):
    def test_later_expiry_is_loosened(self):
        (c,) = _changes(_base(ttl="2027-01-01"), _base(ttl="2028-01-01"))
        self.assertEqual(c.kind, "expiry changed")
        self.assertEqual(c.direction, "loosened")
        self.assertIn("(later)", c.detail)

    def test_earlier_expiry_is_tightened(self):
        (c,) = _changes(_base(ttl="2027-01-01"), _base(ttl="2026-01-01"))
        self.assertEqual(c.direction, "tightened")
        self.assertIn("(earlier)", c.detail)

    def test_removed_expiry_is_loosened(self):
        (c,) = _changes(_base(ttl="2027-01-01"), _base(ttl=None))
        self.assertEqual(c.kind, "expiry removed")
        self.assertEqual(c.direction, "loosened")

    def test_added_expiry_is_tightened(self):
        (c,) = _changes(_base(ttl=None), _base(ttl="2027-01-01"))
        self.assertEqual(c.kind, "expiry added")
        self.assertEqual(c.direction, "tightened")

    def test_same_date_is_not_a_change(self):
        self.assertEqual(_changes(_base(ttl="2027-01-01"),
                                  _base(ttl="2027-01-01")), [])


class Delegations(unittest.TestCase):
    @staticmethod
    def _with_grants(dels):
        return _base(grants={"gmail.send": {"rate": {"max": 100}},
                             "payments.charge": {"amount": {"max": 500}}},
                     delegations=dels)

    def test_delegation_added_is_loosened(self):
        old = self._with_grants([])
        new = self._with_grants([{"from": "a", "to": "b", "purpose": "p",
                                  "narrower_than": {"payments.charge": {"amount": {"max": 50}}}}])
        (c,) = [x for x in _changes(old, new) if x.group == "delegation"]
        self.assertEqual(c.kind, "delegation added")
        self.assertEqual(c.direction, "loosened")
        self.assertEqual(c.sensitive, "payments")
        self.assertTrue(is_gated(c))

    def test_delegation_removed_is_tightened(self):
        old = self._with_grants([{"from": "a", "to": "b", "purpose": "p",
                                  "narrower_than": {"gmail.send": {"rate": {"max": 10}}}}])
        new = self._with_grants([])
        (c,) = [x for x in _changes(old, new) if x.group == "delegation"]
        self.assertEqual(c.kind, "delegation removed")
        self.assertEqual(c.direction, "tightened")

    def test_delegation_widened_child_cap_is_loosened(self):
        dels = [{"from": "a", "to": "b", "purpose": "p",
                 "narrower_than": {"payments.charge": {"amount": {"max": 50}}}}]
        wider = [{"from": "a", "to": "b", "purpose": "p",
                  "narrower_than": {"payments.charge": {"amount": {"max": 500}}}}]
        (c,) = [x for x in _changes(self._with_grants(dels),
                                    self._with_grants(wider))
                if x.group == "delegation"]
        self.assertEqual(c.kind, "delegation changed")
        self.assertEqual(c.direction, "loosened")
        self.assertIn("amount.max 50 -> 500", c.detail)

    def test_identical_delegation_is_not_a_change(self):
        dels = [{"from": "a", "to": "b", "purpose": "p",
                 "narrower_than": {"gmail.send": {"rate": {"max": 10}}}}]
        self.assertEqual([x for x in _changes(self._with_grants(dels),
                                              self._with_grants(dels))
                          if x.group == "delegation"], [])


class Audit(unittest.TestCase):
    def test_dropping_a_payments_receipt_is_a_gated_loosening(self):
        old = _base(audit={"receipt_of": ["payments.charge"]})
        new = _base()
        (c,) = _changes(old, new)
        self.assertEqual(c.kind, "receipt dropped")
        self.assertEqual(c.direction, "loosened")
        self.assertEqual(c.sensitive, "payments")
        self.assertTrue(is_gated(c))

    def test_adding_a_receipt_is_tightened(self):
        (c,) = _changes(_base(), _base(audit={"receipt_of": ["payments.charge"]}))
        self.assertEqual(c.kind, "receipt added")
        self.assertEqual(c.direction, "tightened")


class Gate(unittest.TestCase):
    def test_sensitive_classes_are_payments_fs_shell(self):
        for key, cls in (("payments.charge", "payments"),
                         ("fs.write", "fs"),
                         ("files.delete", "fs"),
                         ("shell.exec", "shell")):
            with self.subTest(key=key):
                old = _base()
                new = _base(grants=dict(_base()["grants"], **{key: True}))
                (c,) = [x for x in _changes(old, new) if x.area == key]
                self.assertEqual(c.sensitive, cls)
                self.assertTrue(is_gated(c))

    def test_non_sensitive_loosening_is_reported_but_not_gated(self):
        old = _base(grants={"gmail.send": {"rate": {"max": 10}}})
        new = _base(grants={"gmail.send": {"rate": {"max": 1000}}})
        (c,) = _changes(old, new)
        self.assertEqual(c.direction, "loosened")
        self.assertFalse(is_gated(c))
        self.assertEqual(gate_hits(_changes(old, new)), [])

    def test_tightening_never_gates(self):
        changes = diff_files(BEFORE, TIGHTENED)
        self.assertTrue(changes)
        self.assertEqual(gate_hits(changes), [])
        self.assertEqual(summarize(changes)["gate_hits"], 0)

    def test_loosened_fixture_gates_on_payments_and_shell(self):
        changes = diff_files(BEFORE, LOOSENED)
        hits = {c.area for c in gate_hits(changes)}
        self.assertEqual(hits, {"payments.charge", "shell.exec", "deny:shell.exec"})

    def test_json_payload_carries_gate(self):
        payload = json.loads(as_json("a", "b", diff_files(BEFORE, LOOSENED)))
        self.assertTrue(payload["gate"]["triggered"])
        self.assertIn("payments.charge", payload["gate"]["hits"])
        self.assertTrue(payload["gate"]["strict_would_fail"])
        self.assertEqual(payload["summary"]["total"], len(payload["changes"]))


class RenderAndDeterminism(unittest.TestCase):
    def test_render_is_deterministic(self):
        changes = diff_files(BEFORE, LOOSENED)
        self.assertEqual(render("a", "b", changes), render("a", "b", changes))

    def test_diff_is_deterministic(self):
        self.assertEqual(diff_files(BEFORE, LOOSENED),
                         diff_files(BEFORE, LOOSENED))

    def test_json_is_deterministic(self):
        changes = diff_files(BEFORE, LOOSENED)
        self.assertEqual(as_json("a", "b", changes), as_json("a", "b", changes))

    def test_render_has_header_table_and_summary(self):
        text = render("before.yaml", "after.yaml", diff_files(BEFORE, LOOSENED))
        self.assertIn("== manifesto diff | before.yaml -> after.yaml", text)
        self.assertIn("AREA", text)
        self.assertIn("DETAIL", text)
        self.assertIn("5 change(s) total", text)
        self.assertIn("!! gate:", text)

    def test_no_changes_renders_cleanly(self):
        text = render("a.yaml", "a.yaml", diff_files(BEFORE, BEFORE))
        self.assertIn("no changes", text)
        self.assertNotIn("!! gate:", text)

    def test_non_mapping_sides_raise(self):
        with self.assertRaises(DiffError):
            diff_manifests(["not", "a", "mapping"], {"agent": "x"})


class CliExitCodes(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_loosening_strict_exits_1(self):
        rc, out = self._run(["diff", str(BEFORE), str(LOOSENED), "--strict"])
        self.assertEqual(rc, 1)
        self.assertIn("GATE (--strict)", out)

    def test_loosening_allow_loosening_exits_0(self):
        rc, out = self._run(["diff", str(BEFORE), str(LOOSENED), "--strict",
                             "--allow-loosening"])
        self.assertEqual(rc, 0)
        self.assertIn("accepted via --allow-loosening", out)

    def test_loosening_without_strict_exits_0(self):
        rc, out = self._run(["diff", str(BEFORE), str(LOOSENED)])
        self.assertEqual(rc, 0)
        self.assertIn("!! gate:", out)      # still reported

    def test_tightening_strict_exits_0(self):
        rc, out = self._run(["diff", str(BEFORE), str(TIGHTENED), "--strict"])
        self.assertEqual(rc, 0)
        self.assertNotIn("GATE (--strict)", out)

    def test_identical_strict_exits_0(self):
        rc, _ = self._run(["diff", str(BEFORE), str(BEFORE), "--strict"])
        self.assertEqual(rc, 0)

    def test_json_output_is_machine_readable(self):
        rc, out = self._run(["diff", str(BEFORE), str(LOOSENED), "--json",
                             "--strict"])
        payload = json.loads(out)
        self.assertEqual(rc, 1)                     # gate still fails in JSON mode
        self.assertTrue(payload["gate"]["triggered"])
        self.assertFalse(payload["gate"]["allow_loosening"])
        self.assertEqual(payload["old"], str(BEFORE))

    def test_missing_file_exits_2(self):
        rc, _ = self._run(["diff", str(BEFORE), "no/such/file.yaml"])
        self.assertEqual(rc, 2)

    def test_unknown_flag_exits_2(self):
        rc, _ = self._run(["diff", str(BEFORE), str(LOOSENED), "--nope"])
        self.assertEqual(rc, 2)


class FixturesStayValid(unittest.TestCase):
    def test_diff_fixtures_all_validate_clean(self):
        for name in ("before", "after-tightened", "after-loosened"):
            with self.subTest(name=name):
                m = load_manifest_file(DIFF_FIXTURES / f"{name}.yaml")
                errors, _ = split_findings(validate(m))
                self.assertEqual(errors, [], f"{name}: {errors}")


if __name__ == "__main__":
    unittest.main()
