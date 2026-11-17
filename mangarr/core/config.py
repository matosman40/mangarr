"""Configuration par variables d'environnement (lues à chaque appel : facile à tester)."""
import os
from dataclasses import dataclass, field


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    library_dir: str = "/library"        # tomes terminés : <library>/<manga>
    config_dir: str = "/config"          # base SQLite, cache, covers, plugins, exports Patreon
    chapters_dir: str = ""               # chapitres en cours (défaut : <library>/_chapters)
    port: int = 8080
    check_interval_min: int = 360        # 0 = planificateur désactivé
    timezone: str = "UTC"
    page_delay: float = 0.3
    chapter_delay: float = 2.0
    max_pages: int = 250
    mangadex_api: str = "https://api.mangadex.org"

    def __post_init__(self):
        if not self.chapters_dir:
            self.chapters_dir = os.path.join(self.library_dir, "_chapters")

    @property
    def db_path(self):
        return os.path.join(self.config_dir, "mangarr.db")

    @property
    def cache_dir(self):
        return os.path.join(self.config_dir, "cache")

    @property
    def covers_dir(self):
        return os.path.join(self.config_dir, "covers")

    @property
    def plugins_dir(self):
        return os.path.join(self.config_dir, "plugins")

    @property
    def patreon_dir(self):
        return os.path.join(self.config_dir, "patreon")


def load_settings() -> Settings:
    e = os.environ
    return Settings(
        library_dir=e.get("MANGARR_LIBRARY", "/library"),
        config_dir=e.get("MANGARR_CONFIG", "/config"),
        chapters_dir=e.get("MANGARR_CHAPTERS", ""),
        port=_int("MANGARR_PORT", 8080),
        check_interval_min=_int("MANGARR_CHECK_INTERVAL", 360),
        timezone=e.get("TZ", "UTC"),
        page_delay=_float("MANGARR_PAGE_DELAY", 0.3),
        chapter_delay=_float("MANGARR_CHAPTER_DELAY", 2.0),
        max_pages=_int("MANGARR_MAX_PAGES", 250),
        mangadex_api=e.get("MANGARR_MANGADEX_API", "https://api.mangadex.org"),
    )
