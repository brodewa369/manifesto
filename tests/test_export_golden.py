"""Golden-file determinism for every export dialect.

For every example manifest and every export target there is a committed
``fixtures/golden/<name>.<target>.expected`` file. The exporter's live output
must match those bytes **exactly** — proving the output is deterministic and
that any change to the emitter is caught by a diff.

Regenerate deliberately (and then review the diff) with::

    MANIFESTO_REGOLDEN=1 python -m unittest tests.test_export_golden
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.export import TARGETS, export  # noqa: E402
from manifesto.schema import load_manifest_file  # noqa: E402

GOLDEN = ROOT / "fixtures" / "golden"
EXAMPLES = ("sales-ops-bot", "coding-agent", "personal-assistant", "delegating-lead")
_REGOLDEN = bool(os.environ.get("MANIFESTO_REGOLDEN"))


class ExportGolden(unittest.TestCase):
    def _check(self, name, target):
        manifest = load_manifest_file(ROOT / "examples" / f"{name}.yaml")
        live = export(manifest, target)
        expected_path = GOLDEN / f"{name}.{target}.expected"

        if _REGOLDEN:
            expected_path.write_text(live, encoding="utf-8")
            return

        self.assertTrue(expected_path.is_file(),
                        f"missing golden file {expected_path}; "
                        "regenerate with MANIFESTO_REGOLDEN=1")
        expected = expected_path.read_text(encoding="utf-8")
        self.assertEqual(
            live.encode(), expected.encode(),
            f"{name}/{target} export drifted from its golden file "
            f"({expected_path.name}); if intentional, regenerate + review")

    def test_agentward_goldens(self):
        for name in EXAMPLES:
            with self.subTest(name=name, target="agentward"):
                self._check(name, "agentward")

    def test_denylist_goldens(self):
        for name in EXAMPLES:
            with self.subTest(name=name, target="denylist"):
                self._check(name, "denylist")

    def test_cedar_goldens(self):
        for name in EXAMPLES:
            with self.subTest(name=name, target="cedar"):
                self._check(name, "cedar")

    def test_every_example_has_goldens_for_every_target(self):
        for name in EXAMPLES:
            for target in TARGETS:
                with self.subTest(name=name, target=target):
                    self.assertTrue((GOLDEN / f"{name}.{target}.expected").is_file())

    def test_live_export_is_deterministic(self):
        # exporting twice yields identical bytes, independent of the goldens
        m = load_manifest_file(ROOT / "examples" / "coding-agent.yaml")
        for target in TARGETS:
            self.assertEqual(export(m, target), export(m, target))


if __name__ == "__main__":
    unittest.main()
