#!/usr/bin/env python3
"""Unit tests for multi-report OTA publish threshold."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.env = {
            "WG_PRON_DATA": str(self.data),
            "WG_PRON_REPORT_THRESHOLD": "10",
            "ANTHROPIC_API_KEY": "",  # force Claude skip
        }
        self._patch = mock.patch.dict(os.environ, self.env, clear=False)
        self._patch.start()
        # Fresh module so DATA_ROOT / threshold pick up env.
        if "app" in sys.modules:
            del sys.modules["app"]
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        self.app_mod = importlib.import_module("app")

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()
        if "app" in sys.modules:
            del sys.modules["app"]

    def test_env_threshold_is_ten(self) -> None:
        self.assertEqual(self.app_mod.REPORT_PUBLISH_THRESHOLD, 10)

    def test_first_report_recorded_returns_speak_form_no_pack(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("ideeohsinkruhsee", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                before = tc.get("/v1/speak-rewrites").json()
                r = tc.post(
                    "/v1/pronunciation-reports",
                    json={
                        "id": "r1",
                        "spelling": "idiosyncrasy",
                        "spokenForm": "idiosyncrasy",
                        "reporterId": "install-a",
                    },
                )
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual(body["status"], "recorded")
                # Local apply on the reporting device; pack stays untouched.
                self.assertEqual(body.get("speakForm"), "ideeohsinkruhsee")
                self.assertFalse(body.get("vetted"))
                self.assertEqual(body["reportCount"], 1)
                self.assertEqual(body["threshold"], 10)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertNotIn("idiosyncrasy", pack.get("exact") or {})
                self.assertEqual(pack.get("packVersion"), before.get("packVersion"))

    def test_ninth_distinct_reporter_still_recorded(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("ideeohsinkruhsee", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                for i in range(9):
                    r = tc.post(
                        "/v1/pronunciation-reports",
                        json={
                            "id": f"r{i}",
                            "spelling": "idiosyncrasy",
                            "spokenForm": "idiosyncrasy",
                            "reporterId": f"install-{i}",
                        },
                    )
                    body = r.json()
                    self.assertEqual(body["status"], "recorded")
                    self.assertFalse(body.get("vetted"))
                    self.assertEqual(body["reportCount"], i + 1)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertNotIn("idiosyncrasy", pack.get("exact") or {})

    def test_tenth_distinct_reporter_publishes(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("ideeohsinkruhsee", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                for i in range(9):
                    tc.post(
                        "/v1/pronunciation-reports",
                        json={
                            "id": f"r{i}",
                            "spelling": "idiosyncrasy",
                            "spokenForm": "idiosyncrasy",
                            "reporterId": f"install-{i}",
                        },
                    )
                r10 = tc.post(
                    "/v1/pronunciation-reports",
                    json={
                        "id": "r9",
                        "spelling": "idiosyncrasy",
                        "spokenForm": "idiosyncrasy",
                        "reporterId": "install-9",
                    },
                )
                body = r10.json()
                self.assertEqual(body["status"], "published")
                self.assertEqual(body["speakForm"], "ideeohsinkruhsee")
                self.assertTrue(body["vetted"])
                self.assertEqual(body["reportCount"], 10)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertEqual(pack["exact"]["idiosyncrasy"], "ideeohsinkruhsee")
                self.assertIn("idiosyncrasy", pack.get("vetted") or [])
                # All devices: GET correction is live so ensureForSpelling
                # / OTA adopt marks isVetted and hides the report button.
                corr = tc.get("/v1/corrections/idiosyncrasy").json()
                self.assertEqual(corr["speakForm"], "ideeohsinkruhsee")
                self.assertTrue(corr["vetted"])

    def test_same_reporter_does_not_publish(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("shohfur", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                for i in range(12):
                    r = tc.post(
                        "/v1/pronunciation-reports",
                        json={
                            "id": f"r{i}",
                            "spelling": "plaid",
                            "spokenForm": "plaid",
                            "reporterId": "same-install",
                        },
                    )
                    body = r.json()
                    self.assertEqual(body["status"], "recorded")
                    self.assertEqual(body["speakForm"], "shohfur")
                    self.assertFalse(body.get("vetted"))
                    self.assertEqual(body["reportCount"], 1)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertNotIn("plaid", pack.get("exact") or {})

    def test_lasagna_operator_lock_still_publishes(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self.app_mod.app) as tc:
            r = tc.post(
                "/v1/pronunciation-reports",
                json={
                    "id": "r-las",
                    "spelling": "lasagna",
                    "spokenForm": "lasagna",
                    "reporterId": "install-a",
                },
            )
            body = r.json()
            self.assertEqual(body["status"], "locked")
            self.assertEqual(body["speakForm"], "luhzahnya")
            self.assertTrue(body["vetted"])
            pack = tc.get("/v1/speak-rewrites").json()
            self.assertEqual(pack["exact"]["lasagna"], "luhzahnya")

    def test_health_exposes_threshold(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self.app_mod.app) as tc:
            body = tc.get("/health").json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["reportPublishThreshold"], 10)

    def test_tally_file_written(self) -> None:
        from fastapi.testclient import TestClient

        with TestClient(self.app_mod.app) as tc:
            tc.post(
                "/v1/pronunciation-reports",
                json={
                    "id": "r1",
                    "spelling": "lantern",
                    "spokenForm": "lantern",
                    "reporterId": "a",
                },
            )
        tally_path = self.data / "report_tally.json"
        self.assertTrue(tally_path.exists())
        tally = json.loads(tally_path.read_text())
        self.assertIn("lantern", tally)
        self.assertEqual(tally["lantern"]["reporters"], ["a"])


if __name__ == "__main__":
    unittest.main()
