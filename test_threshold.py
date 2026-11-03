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
            "WG_PRON_REPORT_THRESHOLD": "2",
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

    def test_first_report_recorded_returns_speak_form_no_pack(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("shohfur", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                r = tc.post(
                    "/v1/pronunciation-reports",
                    json={
                        "id": "r1",
                        "spelling": "chauffeur",
                        "spokenForm": "chauffeur",
                        "reporterId": "install-a",
                    },
                )
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertEqual(body["status"], "recorded")
                # Local apply on the reporting device; pack stays untouched.
                self.assertEqual(body.get("speakForm"), "shohfur")
                self.assertFalse(body.get("vetted"))
                self.assertEqual(body["reportCount"], 1)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertNotIn("chauffeur", pack.get("exact") or {})

    def test_second_distinct_reporter_publishes(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("shohfur", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                tc.post(
                    "/v1/pronunciation-reports",
                    json={
                        "id": "r1",
                        "spelling": "chauffeur",
                        "spokenForm": "chauffeur",
                        "reporterId": "install-a",
                    },
                )
                r2 = tc.post(
                    "/v1/pronunciation-reports",
                    json={
                        "id": "r2",
                        "spelling": "chauffeur",
                        "spokenForm": "chauffeur",
                        "reporterId": "install-b",
                    },
                )
                body = r2.json()
                self.assertEqual(body["status"], "published")
                self.assertEqual(body["speakForm"], "shohfur")
                self.assertTrue(body["vetted"])
                self.assertEqual(body["reportCount"], 2)
                pack = tc.get("/v1/speak-rewrites").json()
                self.assertEqual(pack["exact"]["chauffeur"], "shohfur")
                self.assertIn("chauffeur", pack.get("vetted") or [])

    def test_same_reporter_does_not_publish(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.object(
            self.app_mod,
            "_claude_respell",
            return_value=("shohfur", "raw"),
        ):
            with TestClient(self.app_mod.app) as tc:
                for i in range(3):
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
