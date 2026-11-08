"""Presentation localization must not rewrite user data or leak across requests."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.i18n import language, translate

ORIGIN = "http://127.0.0.1:8080"


class LocalizationTests(unittest.TestCase):
    def test_default_and_explicit_language(self):
        self.assertEqual(translate("Проект не найден."), "Project not found.")
        token = language.set("ru")
        try:
            self.assertEqual(translate("Проект не найден."), "Проект не найден.")
        finally:
            language.reset(token)
        self.assertEqual(translate("Проект не найден."), "Project not found.")

    def test_unknown_text_is_not_machine_translated(self):
        text = "Приватный документ с неизменяемой цитатой."
        self.assertEqual(translate(text), text)

    def test_api_errors_are_request_local_and_default_to_english(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(Path(directory), ORIGIN), base_url=ORIGIN) as client:
                english = client.get("/api/v1/projects")
                russian = client.get("/api/v1/projects", headers={"X-UI-Language": "ru"})
                fallback = client.get("/api/v1/projects", headers={"X-UI-Language": "xx"})
                next_request = client.get("/api/v1/projects")
                self.assertEqual(english.status_code, 401)
                self.assertEqual(english.headers["Content-Language"], "en")
                self.assertEqual(russian.headers["Content-Language"], "ru")
                self.assertEqual(english.json()["message"], "Open the one-time local sign-in link.")
                self.assertEqual(
                    russian.json()["message"], "Откройте одноразовую ссылку локального входа."
                )
                self.assertEqual(fallback.json()["message"], english.json()["message"])
                self.assertEqual(next_request.json()["message"], english.json()["message"])

    def test_locale_header_does_not_change_stored_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root, ORIGIN), base_url=ORIGIN) as client:
                token = (root / "bootstrap.token").read_text()
                session = client.post(
                    "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
                ).json()
                headers = {
                    "Origin": ORIGIN,
                    "X-CSRF-Token": session["csrf_token"],
                    "X-UI-Language": "en",
                }
                name = "Проект не найден."
                response = client.post("/api/v1/projects", json={"name": name}, headers=headers)
                self.assertEqual(response.status_code, 201, response.text)
                for locale in ("en", "ru"):
                    rows = client.get("/api/v1/projects", headers={"X-UI-Language": locale}).json()
                    self.assertEqual(rows[0]["name"], name)
