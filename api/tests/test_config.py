"""Unit tests for Plutus runtime settings and secret-file loading."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings

DB_ENV_VARS = (
    "PLUTUS_DATABASE_URL",
    "PLUTUS_DB_HOST",
    "PLUTUS_DB_PORT",
    "PLUTUS_DB_NAME",
    "PLUTUS_DB_USER",
    "PLUTUS_DB_PASSWORD",
    "PLUTUS_DB_PASSWORD_FILE",
)


def _clean_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in DB_ENV_VARS}
    if overrides:
        env.update(overrides)
    return env


class TestDatabaseConfig(unittest.TestCase):
    def test_db_password_file_builds_url_encoded_dsn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            secret_file = Path(tmp_dir) / "plutus_db_password"
            secret_file.write_text("p@ss/word:123\n", encoding="utf-8")
            env = _clean_env(
                {
                    "PLUTUS_DB_HOST": "plutus-db",
                    "PLUTUS_DB_PORT": "5432",
                    "PLUTUS_DB_NAME": "plutus",
                    "PLUTUS_DB_USER": "plutus@admin",
                    "PLUTUS_DB_PASSWORD_FILE": str(secret_file),
                }
            )
            with patch.dict(os.environ, env, clear=True):
                loaded = Settings()
                self.assertEqual(loaded.db_password, "p@ss/word:123")
                self.assertEqual(
                    loaded.database_url,
                    "postgresql+psycopg://plutus%40admin:p%40ss%2Fword%3A123@plutus-db:5432/plutus",
                )

    def test_explicit_database_url_takes_precedence_over_password_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            secret_file = Path(tmp_dir) / "plutus_db_password"
            secret_file.write_text("p@ss/word:123", encoding="utf-8")
            sqlite_url = f"sqlite:///{Path(tmp_dir) / 'override.db'}"
            env = _clean_env(
                {
                    "PLUTUS_DATABASE_URL": sqlite_url,
                    "PLUTUS_DB_HOST": "plutus-db",
                    "PLUTUS_DB_PASSWORD_FILE": str(secret_file),
                }
            )
            with patch.dict(os.environ, env, clear=True):
                loaded = Settings()
                self.assertEqual(loaded.database_url, sqlite_url)
                self.assertEqual(loaded.db_password, "p@ss/word:123")

    def test_default_database_url_preserved_without_password_or_url(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            loaded = Settings()
            self.assertEqual(
                loaded.database_url,
                "postgresql+psycopg://plutus:plutus@localhost:5432/plutus",
            )

    def test_missing_db_password_file_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing_file = Path(tmp_dir) / "does_not_exist"
            env = _clean_env({"PLUTUS_DB_PASSWORD_FILE": str(missing_file)})
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "not found"):
                    Settings()

    def test_empty_db_password_file_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_file = Path(tmp_dir) / "empty_password"
            empty_file.write_text("   \n", encoding="utf-8")
            env = _clean_env({"PLUTUS_DB_PASSWORD_FILE": str(empty_file)})
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, "empty"):
                    Settings()

    def test_webhook_secret_file_and_dbs_config_parsers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            secret_file = Path(tmp_dir) / "plutus_webhook_secret"
            secret_file.write_text("iris-hook-secret\n", encoding="utf-8")
            loaded = Settings(
                webhook_secret="env-fallback",
                webhook_secret_file=str(secret_file),
                dbs_account_map='{"1111": 4, "2222": 6}',
                own_account_suffixes="1111, 2222, 3333",
            )
            self.assertEqual(loaded.resolved_webhook_secret, "iris-hook-secret")
            self.assertEqual(loaded.resolved_dbs_account_map(), {"1111": 4, "2222": 6})
            self.assertEqual(loaded.resolved_own_account_suffixes(), {"1111", "2222", "3333"})

            fallback = Settings(webhook_secret="env-only", webhook_secret_file=str(Path(tmp_dir) / "missing"))
            self.assertEqual(fallback.resolved_webhook_secret, "env-only")

            bad_json = Settings(dbs_account_map="{bad-json")
            with self.assertRaises(ValueError):
                bad_json.resolved_dbs_account_map()

            bad_type = Settings(dbs_account_map='{"1111": "not-an-int"}')
            with self.assertRaises(ValueError):
                bad_type.resolved_dbs_account_map()

            bad_bool = Settings(dbs_account_map='{"1111": true}')
            with self.assertRaises(ValueError):
                bad_bool.resolved_dbs_account_map()


if __name__ == "__main__":
    unittest.main()
