"""Runtime configuration: where the app files and the shelf live.

Replaces the module-level constants the original script defined at import
time, so the paths can be pointed at a temp directory in tests and overridden
by --db / --root on the command line.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "gpt-4.1-mini"

#: App directory — this package's parent. Holds the DB, the config file, the
#: web UI, and vendor/. The shelf itself lives under data/.
APP_DIR = Path(__file__).resolve().parent.parent

LANDING_NAME = "__landing"


class ConfigError(Exception):
    """Configuration is missing or unusable; the CLI reports this and exits."""


@dataclass(frozen=True)
class Config:
    """Resolved paths for one invocation.

    `data_dir` is the shelf root: category directories and the landing pad
    live directly under it, and catalog `directory` keys are stored relative
    to it.
    """

    app_dir: Path
    data_dir: Path
    db_path: Path
    config_file: Path
    model: str = DEFAULT_MODEL

    def __post_init__(self):
        # key()/path() compare resolved paths against data_dir, so every path
        # here must already be resolved — including when Config is constructed
        # directly (tests, embedding) rather than via default()/from_args().
        for field in ("app_dir", "data_dir", "db_path", "config_file"):
            object.__setattr__(self, field, Path(getattr(self, field)).resolve())

    @property
    def landing_dir(self) -> Path:
        return self.data_dir / LANDING_NAME

    @classmethod
    def default(cls) -> "Config":
        return cls(
            app_dir=APP_DIR,
            data_dir=APP_DIR / "data",
            db_path=APP_DIR / ".rename_books.db",
            config_file=APP_DIR / ".rename_books.toml",
        )

    @classmethod
    def from_args(cls, args) -> "Config":
        """Build from parsed argparse options, honouring --db and --root."""
        base = cls.default()

        def pick(name, fallback):
            value = getattr(args, name, None)
            return Path(value).expanduser().resolve() if value else fallback

        return cls(
            app_dir=base.app_dir,
            data_dir=pick("root", base.data_dir),
            db_path=pick("db", base.db_path),
            config_file=pick("config", base.config_file),
            model=getattr(args, "model", None) or base.model,
        )

    def with_(self, **changes) -> "Config":
        """A copy with fields replaced — convenience for tests."""
        from dataclasses import replace

        return replace(self, **changes)

    # ── secrets ──────────────────────────────────────────────────────────

    def api_key(self) -> str:
        """The OpenAI key, from the config file or OPENAI_API_KEY.

        Raises ConfigError rather than exiting, so callers (and tests) decide
        how to report it.
        """
        if self.config_file.exists():
            with open(self.config_file, "rb") as f:
                data = tomllib.load(f)
            key = data.get("openai", {}).get("api_key", "")
            if key and not key.startswith("sk-..."):
                return key

        key = os.environ.get("OPENAI_API_KEY", "")
        if key:
            return key

        raise ConfigError(
            f"No API key found. Set it in {self.config_file} "
            f"or via the OPENAI_API_KEY environment variable."
        )
