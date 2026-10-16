"""Discovery-scan tests — run against ``fixtures/home`` only.

The real ``~/.hermes`` etc. are never touched: every test passes an explicit
``--home`` fixture directory.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.scan import infer_grants, render, scan  # noqa: E402

FIXTURE_HOME = ROOT / "fixtures" / "home"


class ScanFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = scan(home=FIXTURE_HOME)
        cls.by_client = {c["client"]: c for c in cls.report["clients"]}

    def test_all_clients_present(self):
        self.assertEqual(set(self.by_client), {"hermes", "claude", "cursor", "vscode"})

    def test_hermes_servers_parsed(self):
        hermes = self.by_client["hermes"]
        self.assertEqual(hermes["servers"],
                         ["broken-shell", "filesystem", "github", "semantic-vault"])
        self.assertTrue(hermes["present"])

    def test_credentials_detected_by_env_name(self):
        # credential detection keys off the env VAR NAME, never the value
        creds = {(c["server"], c["env"]) for c in self.by_client["hermes"]["credentials"]}
        self.assertIn(("github", "GITHUB_TOKEN"), creds)
        self.assertIn(("broken-shell", "SHELL_API_KEY"), creds)

    def test_provenance_points_at_source_file(self):
        prov = self.by_client["hermes"]["provenance"]
        self.assertTrue(all(p.endswith(".hermes/config.yaml") for p in prov.values()))
        self.assertIn("github.read", prov)
        self.assertIn("fs.read", prov)

    def test_draft_grants_inferred(self):
        grants = self.by_client["hermes"]["draft"]["grants"]
        self.assertIn("shell.exec", grants)      # broken-shell -> shell.exec
        self.assertIn("github.write", grants)
        self.assertIn("fs.read", grants)

    def test_no_manifest_finding_emitted(self):
        codes = {f["code"] for f in self.report["findings"]}
        self.assertIn("no_manifest", codes)
        self.assertTrue(all("no manifest on file" in f["message"]
                            for f in self.report["findings"] if f["code"] == "no_manifest"))

    def test_git_user_read(self):
        self.assertEqual(self.report["git_user"]["name"], "Synthetic User")
        self.assertEqual(self.report["git_user"]["email"], "synthetic@example.com")

    def test_crontab_read_from_fixture(self):
        self.assertEqual(len(self.report["crontab"]["lines"]), 2)

    def test_scan_is_json_serializable(self):
        json.dumps(self.report, default=str)  # must not raise

    def test_render_mentions_every_client(self):
        text = render(self.report)
        for cl in ("hermes", "claude", "cursor", "vscode"):
            self.assertIn(cl, text)


class SingleClient(unittest.TestCase):
    def test_restricted_to_one_client(self):
        rep = scan(home=FIXTURE_HOME, client="cursor")
        self.assertEqual([c["client"] for c in rep["clients"]], ["cursor"])
        self.assertEqual(rep["clients"][0]["servers"], ["stripe"])


class GrantInference(unittest.TestCase):
    def test_known_server_maps_to_system(self):
        keys = [k for k, _ in infer_grants("github-mcp")]
        self.assertIn("github.read", keys)

    def test_unknown_server_is_conservative_read(self):
        keys = [k for k, _ in infer_grants("some-unknown-thing")]
        self.assertEqual(keys, ["some_unknown_thing.read"])


if __name__ == "__main__":
    unittest.main()
