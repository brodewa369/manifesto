"""Export determinism + CLI exit-code contract."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto import cli  # noqa: E402
from manifesto.export import export_agentward, export_denylist  # noqa: E402
from manifesto.schema import load_manifest_file  # noqa: E402

EXAMPLE = ROOT / "examples" / "coding-agent.yaml"


class ExportDeterminism(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest_file(EXAMPLE)

    def test_agentward_same_bytes(self):
        self.assertEqual(export_agentward(self.manifest).encode(),
                         export_agentward(self.manifest).encode())

    def test_denylist_same_bytes(self):
        self.assertEqual(export_denylist(self.manifest).encode(),
                         export_denylist(self.manifest).encode())

    def test_denylist_sorted_and_stable(self):
        a = export_denylist(self.manifest)
        b = export_denylist(self.manifest)
        self.assertEqual(a, b)
        payload = json.loads(a)
        self.assertEqual(payload["deny"], sorted(payload["deny"]))
        self.assertEqual(payload["agent"], "coding-agent")

    def test_agentward_has_disclaimer(self):
        out = export_agentward(self.manifest)
        self.assertIn("APPROXIMATION", out)
        self.assertIn("approximation", out)

    def test_denylist_has_disclaimer(self):
        payload = json.loads(export_denylist(self.manifest))
        self.assertIn("approximation", payload["disclaimer"])


class CliExitCodes(unittest.TestCase):
    def test_valid_example_exits_0(self):
        self.assertEqual(cli.main(["validate", str(EXAMPLE)]), 0)

    def test_strict_promotes_warnings(self):
        # personal-assistant is clean; craft a manifest with only a warning
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("agent: a\ngrants:\n  gmail.read: true\n")  # missing ttl -> warning
            path = fh.name
        self.assertEqual(cli.main(["validate", path]), 0)          # warning alone -> 0
        self.assertEqual(cli.main(["validate", "--strict", path]), 1)  # strict -> 1

    def test_missing_manifest_exits_2(self):
        self.assertEqual(cli.main(["validate", "no/such/file.yaml"]), 2)

    def test_bad_target_is_usage_error(self):
        # argparse rejects an unknown --target with exit code 2
        self.assertEqual(cli.main(["export", str(EXAMPLE), "--target", "nope"]), 2)

    def test_json_flag_emits_findings(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["validate", "--json", str(EXAMPLE)])
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["error_count"], 0)


class CliScan(unittest.TestCase):
    def test_scan_fixture_home_exits_1_with_findings(self):
        rc = cli.main(["scan", "--home", str(ROOT / "fixtures" / "home"), "--json"])
        self.assertEqual(rc, 1)  # no-manifest findings -> 1


if __name__ == "__main__":
    unittest.main()
