"""Multi-manifest handling — ``validate --dir`` / ``explain --dir``.

Covers: batch loading of a directory, cross-file agent-name collision
detection, per-file findings, an unreadable file surfacing as a fail-closed
error (not skipped), batch exit codes (0 clean, 1 error/collision), JSON
output, determinism, and explain --dir rendering every manifest.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto import cli  # noqa: E402
from manifesto.batch import (discover, load_batch, render_batch,  # noqa: E402
                             render_explain_batch, validate_batch)

FLEET = ROOT / "fixtures" / "fleet"
CLEAN = ROOT / "examples"  # unique agent names, no collisions


class Discover(unittest.TestCase):
    def test_fleet_dir_found(self):
        paths = discover(FLEET)
        names = [p.name for p in paths]
        self.assertEqual(names, ["billing-bot.yaml", "ops-bot.yaml",
                                 "support-bot.yaml"])

    def test_not_a_directory_raises(self):
        from manifesto.schema import ManifestError
        with self.assertRaises(ManifestError):
            discover(FLEET / "billing-bot.yaml")


class Collision(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = validate_batch(FLEET)

    def test_collision_detected(self):
        cols = self.report["collisions"]
        self.assertEqual(len(cols), 1)
        self.assertEqual(cols[0]["agent"], "billing-bot")
        self.assertEqual(len(cols[0]["files"]), 2)

    def test_all_files_valid(self):
        self.assertEqual(self.report["summary"]["valid"], 3)
        self.assertEqual(self.report["summary"]["invalid"], 0)

    def test_collision_counts_as_error(self):
        # 0 validator errors, but the collision is 1 error -> exit 1
        self.assertEqual(self.report["summary"]["collisions"], 1)
        self.assertEqual(self.report["summary"]["errors"], 1)

    def test_clean_dir_has_no_collision(self):
        report = validate_batch(CLEAN)
        self.assertEqual(report["collisions"], [])
        self.assertEqual(report["summary"]["collisions"], 0)


class UnreadableFile(unittest.TestCase):
    def test_broken_manifest_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "good.yaml").write_text("agent: good\ngrants:\n  fs.read: true\n")
            # top level is a bare list, not a mapping -> ManifestError (load_error)
            (p / "bad.yaml").write_text("- just\n- a\n- list\n")
            report = validate_batch(p)
            bad = [e for e in report["entries"] if e["path"].endswith("bad.yaml")]
            self.assertEqual(len(bad), 1)
            self.assertFalse(bad[0]["valid"])
            self.assertEqual(bad[0]["error_count"], 1)
            self.assertIn("load_error", bad[0])
            self.assertEqual(report["summary"]["errors"], 1)


class Render(unittest.TestCase):
    def test_render_mentions_collision(self):
        report = validate_batch(FLEET)
        text = render_batch(report)
        self.assertIn("COLLISION", text)
        self.assertIn("billing-bot", text)

    def test_explain_dir_renders_all(self):
        text = render_explain_batch(FLEET)
        self.assertIn("billing-bot", text)
        self.assertIn("support-bot", text)


class Determinism(unittest.TestCase):
    def test_same_dir_same_report(self):
        a = validate_batch(FLEET)
        b = validate_batch(FLEET)
        self.assertEqual(a, b)


class CliExitCodes(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_validate_dir_collision_exit_1(self):
        code, out = self._run(["validate", "--dir", str(FLEET)])
        self.assertEqual(code, 1)
        self.assertIn("COLLISION", out)

    def test_validate_dir_clean_exit_0(self):
        code, _ = self._run(["validate", "--dir", str(CLEAN)])
        self.assertEqual(code, 0)

    def test_validate_dir_json(self):
        code, out = self._run(["validate", "--dir", str(FLEET), "--json"])
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["collisions"], 1)

    def test_validate_no_args_exit_2(self):
        code, _ = self._run(["validate"])
        self.assertEqual(code, 2)

    def test_explain_dir(self):
        code, out = self._run(["explain", "--dir", str(FLEET)])
        self.assertEqual(code, 0)
        self.assertIn("billing-bot", out)

    def test_explain_no_args_exit_2(self):
        code, _ = self._run(["explain"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
