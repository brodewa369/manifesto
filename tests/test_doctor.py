"""``manifesto test`` — the fleet doctor (drift + phantom/undeclared audit).

Covers: UNDECLARED detection (tool on disk, absent from manifest) with
file:line provenance, PHANTOM detection (grant claimed, no tool found), the
fail-closed skip of unknown servers, sensitive-class flagging, the --strict
gate on sensitive undeclared tools, JSON output, determinism, and CLI exit
codes. Never touches the real home — points at fixtures/home/.
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
from manifesto.doctor import render, test_fleet  # noqa: E402
from manifesto.schema import load_manifest_file  # noqa: E402

HOME = ROOT / "fixtures" / "home"
DRIFT = ROOT / "fixtures" / "drift" / "agent.manifest.yaml"
NOW = date(2026, 6, 1)


def _keys(findings):
    return sorted(f["key"] for f in findings)


class DriftFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest_file(DRIFT)
        cls.report = test_fleet(cls.manifest, home=HOME, now=NOW)

    def test_phantom_detected(self):
        self.assertEqual(_keys(self.report["phantom"]),
                         ["calendar.read", "gmail.send"])

    def test_undeclared_detected(self):
        keys = _keys(self.report["undeclared"])
        self.assertIn("stripe", keys)     # payments (sensitive)
        self.assertIn("broken-shell", keys)  # shell (sensitive)
        self.assertIn("slack", keys)      # slack (not sensitive)

    def test_undeclared_has_provenance(self):
        stripe = next(f for f in self.report["undeclared"] if f["key"] == "stripe")
        self.assertIn(".cursor/mcp.json", stripe["source"])
        self.assertIn(":", stripe["source"])  # file:line

    def test_sensitive_class_flagged(self):
        stripe = next(f for f in self.report["undeclared"] if f["key"] == "stripe")
        self.assertEqual(stripe["sensitive"], "payments")
        shell = next(f for f in self.report["undeclared"]
                     if f["key"] == "broken-shell")
        self.assertEqual(shell["sensitive"], "shell")

    def test_summary_counts(self):
        s = self.report["summary"]
        self.assertEqual(s["phantom"], 2)
        self.assertEqual(s["undeclared"], 4)
        self.assertEqual(s["strict_sensitive"], 2)  # payments + shell
        self.assertEqual(s["total"], 6)

    def test_in_sync_manifest_is_clean(self):
        # a manifest that declares every present system -> no undeclared
        manifest = {"agent": "sync", "grants": {
            "fs.read": True, "github.read": True, "http.get": True,
            "payments.charge": {"amount": {"max": 10, "currency": "USD",
                                           "window": "P1D"}},
            "slack.read": True, "shell.exec": {"approval": "required"}}}
        report = test_fleet(manifest, home=HOME, now=NOW)
        self.assertEqual(report["undeclared"], [])
        self.assertEqual(report["phantom"], [])
        self.assertEqual(report["summary"]["total"], 0)


class RenderAndJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest_file(DRIFT)
        cls.report = test_fleet(cls.manifest, home=HOME, now=NOW)

    def test_render_mentions_both_kinds(self):
        text = render(self.report)
        self.assertIn("UNDECLARED", text)
        self.assertIn("PHANTOM", text)
        self.assertIn("--strict", text)

    def test_json_round_trip(self):
        payload = json.loads(json.dumps(self.report, default=str))
        self.assertEqual(payload["summary"]["total"], 6)


class Determinism(unittest.TestCase):
    def test_same_input_same_report(self):
        m = load_manifest_file(DRIFT)
        a = test_fleet(m, home=HOME, now=NOW)
        b = test_fleet(m, home=HOME, now=NOW)
        self.assertEqual(a, b)


class CliExitCodes(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_default_exit_0_even_with_findings(self):
        # without --strict, sensitive undeclared tools do not fail the build
        code, _ = self._run(["test", str(DRIFT), "--home", str(HOME)])
        self.assertEqual(code, 0)

    def test_strict_exit_1_on_sensitive(self):
        code, out = self._run(["test", str(DRIFT), "--home", str(HOME), "--strict"])
        self.assertEqual(code, 1)
        self.assertIn("UNDECLARED", out)

    def test_json_output(self):
        code, out = self._run(["test", str(DRIFT), "--home", str(HOME), "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["total"], 6)

    def test_unreadable_manifest_exit_2(self):
        code, _ = self._run(["test", "no/such/file.yaml", "--home", str(HOME)])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
