import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from longwatch.config import ensure_alert_link_secret


class EnsureAlertLinkSecretTests(TestCase):
    def setUp(self):
        self._environment = dict(os.environ)
        os.environ.pop("ALERT_LINK_SECRET", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._environment)

    def test_generated_secret_is_persisted_and_reused(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"

            first = ensure_alert_link_secret(env_path)
            self.assertTrue(first)
            self.assertIn(
                f"ALERT_LINK_SECRET={first}", env_path.read_text(encoding="utf-8")
            )

            os.environ.pop("ALERT_LINK_SECRET", None)
            self.assertEqual(ensure_alert_link_secret(env_path), first)

    def test_environment_secret_is_persisted_for_later_restarts(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            os.environ["ALERT_LINK_SECRET"] = "provided-by-environment"

            self.assertEqual(
                ensure_alert_link_secret(env_path), "provided-by-environment"
            )
            self.assertIn(
                "ALERT_LINK_SECRET=provided-by-environment",
                env_path.read_text(encoding="utf-8"),
            )

    def test_stored_value_wins_over_the_environment(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("ALERT_LINK_SECRET=stored\n", encoding="utf-8")
            os.environ["ALERT_LINK_SECRET"] = "provided-by-environment"

            self.assertEqual(ensure_alert_link_secret(env_path), "stored")
            self.assertEqual(
                env_path.read_text(encoding="utf-8"), "ALERT_LINK_SECRET=stored\n"
            )

    def test_existing_lines_are_preserved_when_generating(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# comment\nBARK_GROUP=LongWatch\nALERT_LINK_SECRET=\n",
                encoding="utf-8",
            )

            secret = ensure_alert_link_secret(env_path)
            contents = env_path.read_text(encoding="utf-8")

            self.assertIn("# comment", contents)
            self.assertIn("BARK_GROUP=LongWatch", contents)
            self.assertIn(f"ALERT_LINK_SECRET={secret}", contents)

    def test_unwritable_env_file_still_returns_a_usable_secret(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("BARK_GROUP=LongWatch\n", encoding="utf-8")

            with patch(
                "longwatch.config.os.replace",
                side_effect=PermissionError("read-only file system"),
            ):
                secret = ensure_alert_link_secret(env_path)

            self.assertTrue(secret)
            self.assertEqual(os.environ["ALERT_LINK_SECRET"], secret)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"), "BARK_GROUP=LongWatch\n"
            )

    def test_unwritable_directory_still_returns_a_usable_secret(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / "missing" / ".env"

            with patch(
                "longwatch.config.Path.mkdir",
                side_effect=PermissionError("read-only file system"),
            ):
                secret = ensure_alert_link_secret(env_path)

            self.assertTrue(secret)
            self.assertEqual(os.environ["ALERT_LINK_SECRET"], secret)
