"""Runtime settings, read once from the environment (or a .env file in the data dir)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"BAROMETER_{name}", default)


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader so systemd and dev runs share one file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Europe/London"
    # Claude
    llm: str = "claude"  # "claude" | "fake" | "off"
    model: str = "claude-opus-5-5"
    effort_extract: str = "medium"
    effort_brief: str = "low"
    effort_analysis: str = "medium"
    pseudonymise: bool = True
    # Security
    session_idle_seconds: int = 600
    secure_cookies: bool = True
    # Argon2id cost for the Deep passphrase. "moderate" is ~256 MB / a second or two on a Pi 5.
    kdf_strength: str = "moderate"
    # Voice
    whisper_model: str = "base"
    whisper_cpp_bin: str | None = None
    whisper_cpp_model: str | None = None
    # Notifications and backups
    ntfy_url: str | None = None
    backup_dir: Path | None = None
    backup_public_key: str | None = None
    backup_keep: int = 14
    # Optional SQLCipher key file for the Almanac DB
    almanac_key_file: Path | None = None
    # Mirror
    mirror_weekly_cap: int = 2
    extra: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def almanac_path(self) -> Path:
        return self.data_dir / "almanac.db"

    @property
    def deep_path(self) -> Path:
        return self.data_dir / "deep.db.enc"

    @property
    def deep_keys_path(self) -> Path:
        return self.data_dir / "deep_keys.json"

    @property
    def spool_dir(self) -> Path:
        return self.data_dir / "spool"


def load_settings(data_dir: str | os.PathLike | None = None) -> Settings:
    root = Path(data_dir or _env("DATA_DIR", "./data")).expanduser().resolve()
    _load_dotenv(root / ".env")
    backup_dir = _env("BACKUP_DIR")
    almanac_key = _env("ALMANAC_KEY_FILE")
    return Settings(
        data_dir=root,
        host=_env("HOST", "127.0.0.1"),
        port=int(_env("PORT", "8000")),
        timezone=_env("TZ", "Europe/London"),
        llm=_env("LLM", "claude"),
        model=_env("MODEL", "claude-opus-5-5"),
        effort_extract=_env("EFFORT_EXTRACT", "medium"),
        effort_brief=_env("EFFORT_BRIEF", "low"),
        effort_analysis=_env("EFFORT_ANALYSIS", "medium"),
        pseudonymise=_bool("PSEUDONYMISE", True),
        session_idle_seconds=int(_env("SESSION_IDLE_SECONDS", "600")),
        secure_cookies=_bool("SECURE_COOKIES", True),
        kdf_strength=_env("KDF_STRENGTH", "moderate"),
        whisper_model=_env("WHISPER_MODEL", "base"),
        whisper_cpp_bin=_env("WHISPER_CPP_BIN"),
        whisper_cpp_model=_env("WHISPER_CPP_MODEL"),
        ntfy_url=_env("NTFY_URL"),
        backup_dir=Path(backup_dir).expanduser() if backup_dir else None,
        backup_public_key=_env("BACKUP_PUBLIC_KEY"),
        backup_keep=int(_env("BACKUP_KEEP", "14")),
        almanac_key_file=Path(almanac_key).expanduser() if almanac_key else None,
        mirror_weekly_cap=int(_env("MIRROR_WEEKLY_CAP", "2")),
    )
