import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from spec_enricher.cli import main
from spec_enricher.security import Sessions, create_bootstrap


class BootstrapContractTests(unittest.TestCase):
    def test_specified_cli_command_issues_fragment_token_only_to_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.dict(
                os.environ,
                {"APP_DATA_DIR": directory, "APP_ALLOWED_ORIGIN": "http://127.0.0.1:8080"},
            ):
                with (
                    patch("sys.argv", ["spec-enricher", "session-bootstrap"]),
                    redirect_stdout(output),
                ):
                    main()
            url = urlsplit(output.getvalue().strip())
            self.assertEqual(url.query, "")
            self.assertEqual(
                url.fragment, "bootstrap=" + (Path(directory) / "bootstrap.token").read_text()
            )
            self.assertEqual((Path(directory) / "bootstrap.token").stat().st_mode & 0o777, 0o600)

    def test_bootstrap_expires_after_five_minutes_and_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = Sessions(root)
            token = create_bootstrap(root)
            created = sessions.path.stat().st_mtime
            with patch("spec_enricher.security.time.time", return_value=created + 301):
                self.assertIsNone(sessions.bootstrap(token))
            token = create_bootstrap(root)
            created = sessions.path.stat().st_mtime
            with patch("spec_enricher.security.time.time", return_value=created + 299):
                self.assertIsNotNone(sessions.bootstrap(token))
                self.assertIsNone(sessions.bootstrap(token))
