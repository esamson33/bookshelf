"""Config resolution and API-key lookup."""

import os
import tempfile
import unittest
from pathlib import Path

from bookshelf.config import Config, ConfigError


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Config resolves every path it holds, so resolve the expectation too.
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(
            app_dir=self.root,
            data_dir=self.root / "data",
            db_path=self.root / "cat.db",
            config_file=self.root / "conf.toml",
        )
        self._env = os.environ.get("OPENAI_API_KEY")
        os.environ.pop("OPENAI_API_KEY", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._env

    def test_landing_dir_sits_under_data(self):
        self.assertEqual(self.cfg.landing_dir, self.root / "data" / "__landing")

    def test_key_from_config_file(self):
        self.cfg.config_file.write_text('[openai]\napi_key = "sk-real-key"\n')
        self.assertEqual(self.cfg.api_key(), "sk-real-key")

    def test_placeholder_key_falls_through_to_env(self):
        self.cfg.config_file.write_text('[openai]\napi_key = "sk-..."\n')
        os.environ["OPENAI_API_KEY"] = "sk-from-env"
        self.assertEqual(self.cfg.api_key(), "sk-from-env")

    def test_missing_key_raises_rather_than_exits(self):
        with self.assertRaises(ConfigError):
            self.cfg.api_key()

    def test_from_args_overrides_db_and_root(self):
        class Args:
            db = str(self.root / "other.db")
            root = str(self.root / "shelf")
            model = None

        cfg = Config.from_args(Args())
        # from_args resolves, so compare resolved (macOS /var -> /private/var).
        self.assertEqual(cfg.db_path, (self.root / "other.db").resolve())
        self.assertEqual(cfg.data_dir, (self.root / "shelf").resolve())
        # app_dir and the config file stay put — --db/--root do not relocate secrets.
        self.assertEqual(cfg.config_file, Config.default().config_file)

    def test_from_args_without_overrides_keeps_defaults(self):
        class Args:
            db = None
            root = None
            model = None

        self.assertEqual(Config.from_args(Args()), Config.default())


if __name__ == "__main__":
    unittest.main()
