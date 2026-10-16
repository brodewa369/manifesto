"""Discovery-scan tests — run against synthetic fixture homes only.

The real ``~/.hermes`` etc. are never touched: every test passes an explicit
``--home`` fixture directory. v0.2 asserts the **fail-closed** contract:
inference only on an exact table match; unknown servers yield ``unknown: true``
and never a guessed grant.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from manifesto.scan import infer_grants, render, scan  # noqa: E402

FIXTURE_HOME = ROOT / "fixtures" / "home"
UNKNOWN_HOME = ROOT / "fixtures" / "unknown-home"


class ScanFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = scan(home=FIXTURE_HOME)
        cls.by_client = {c["client"]: c for c in cls.report["clients"]}
        cls.finding_codes = {f["code"] for f in cls.report["findings"]}

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

    def test_provenance_is_file_line_annotated(self):
        prov = self.by_client["hermes"]["provenance"]
        self.assertTrue(prov)
        for src in prov.values():
            self.assertRegex(src, r"\.hermes/config\.yaml:\d+$")
        self.assertIn("github.read", prov)
        self.assertIn("fs.read", prov)

    def test_draft_grants_inferred(self):
        grants = self.by_client["hermes"]["draft"]["grants"]
        self.assertIn("shell.exec", grants)      # broken-shell -> shell.exec
        self.assertIn("github.write", grants)
        self.assertIn("fs.read", grants)
        # exact-match table: no unknown sentinel for the fully-known hermes home
        self.assertNotIn("unknown", grants)

    def test_no_manifest_finding_emitted(self):
        self.assertIn("no_manifest", self.finding_codes)
        self.assertTrue(all("no manifest on file" in f["message"]
                            for f in self.report["findings"] if f["code"] == "no_manifest"))

    def test_credential_orphan_for_credentialed_unmanifested_clients(self):
        # cursor (stripe) and vscode (slack) hold credentials but no manifest
        orphan_clients = {f["client"] for f in self.report["findings"]
                          if f["code"] == "credential-orphan"}
        self.assertIn("cursor", orphan_clients)
        self.assertIn("vscode", orphan_clients)
        # claude has servers but no credential -> not an orphan
        self.assertNotIn("claude", orphan_clients)
        # hermes HAS a manifest on file -> not an orphan
        self.assertNotIn("hermes", orphan_clients)

    def test_hermes_manifest_surfaces_expired_and_unbounded(self):
        self.assertIn("expired-ttl", self.finding_codes)
        self.assertIn("unbounded-grant", self.finding_codes)

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
    def test_known_exact_server_maps_to_system(self):
        d = infer_grants("github-mcp")
        self.assertTrue(d["known"])
        self.assertIn("github.read", d["grants"])

    def test_case_insensitive_exact_match(self):
        self.assertTrue(infer_grants("GitHub")["known"])

    def test_no_substring_matching(self):
        # 'my-github' contains 'github' but is NOT an exact table key -> unknown
        d = infer_grants("my-github")
        self.assertFalse(d["known"])
        self.assertEqual(d["grants"], {})

    def test_unknown_server_is_fail_closed(self):
        d = infer_grants("some-unknown-thing")
        self.assertFalse(d["known"])
        # fail-closed: NO guessed capability grant at all
        self.assertEqual(d["grants"], {})
        self.assertNotIn("some_unknown_thing.read", d["grants"])

    def test_notes_carry_why(self):
        d = infer_grants("github", source="/x/.hermes/config.yaml:3")
        self.assertTrue(any("/x/.hermes/config.yaml:3" in n for n in d["notes"]))


class FailClosedUnknownHome(unittest.TestCase):
    """The core fail-closed property: unknown tools are never guessed."""

    @classmethod
    def setUpClass(cls):
        cls.report = scan(home=UNKNOWN_HOME, client="claude")
        cls.claude = cls.report["clients"][0]

    def test_unknown_server_recorded_not_guessed(self):
        self.assertEqual(self.claude["unknowns"], ["quantum-admin"])
        grants = self.claude["draft"]["grants"]
        # ONLY the unknown sentinel is present; nothing was inferred
        self.assertEqual(set(grants), {"unknown"})
        self.assertIs(grants["unknown"], True)
        # specifically, no read/write capability was invented for it
        self.assertNotIn("quantum_admin.read", grants)
        self.assertNotIn("quantum_admin.write", grants)

    def test_unknown_server_triggers_type_suspect(self):
        self.assertIn("type-suspect", {f["code"] for f in self.report["findings"]})

    def test_credential_orphan_on_unknown_home(self):
        self.assertIn("credential-orphan", {f["code"] for f in self.report["findings"]})


class UnparsableConfig(unittest.TestCase):
    def test_broken_config_is_fail_closed(self):
        import tempfile, os
        home = tempfile.mkdtemp()
        cfg = Path(home) / ".claude"
        cfg.mkdir(parents=True)
        (cfg / "claude_desktop_config.json").write_text("{ this is not json", encoding="utf-8")
        rep = scan(home=home, client="claude")
        entry = rep["clients"][0]
        self.assertEqual(entry["parse_error"], "unparsable")
        # fail-closed draft: unknown:true, nothing inferred
        self.assertEqual(entry["draft"]["grants"], {"unknown": True})
        self.assertIn("unparsable-config", {f["code"] for f in rep["findings"]})
        import shutil
        shutil.rmtree(home, ignore_errors=True)


class TypeSuspectDetection(unittest.TestCase):
    def test_privileged_name_is_suspect_even_when_known(self):
        # a name implying more verbs than inferred
        d = infer_grants("shell-admin")
        self.assertIsNotNone(d.get("type_suspect"))

    def test_clean_name_not_suspect(self):
        d = infer_grants("filesystem")
        self.assertIsNone(d.get("type_suspect"))


if __name__ == "__main__":
    unittest.main()
