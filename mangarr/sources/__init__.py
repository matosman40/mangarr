"""Registre des sources : intégrées, plugins (<config>/plugins/*.py) et sources « formulaire » (base)."""
import importlib.util
import os
import sys

from mangarr.core.net import ChapterMissing, FetchError, fetch_image, fetch_text  # noqa: F401  (API des plugins)
from mangarr.sources.base import SOURCES, Source, register
from mangarr.sources import builtin, patreon  # noqa: F401  (enregistrent les sources)
from mangarr.sources.builtin import HtmlSource, PatternSource

BUILTIN = set(SOURCES)

__all__ = ["Source", "register", "SOURCES", "ChapterMissing", "FetchError", "fetch_image", "fetch_text",
           "load_plugins", "load_custom_sources", "build_source", "searchable_names"]


def searchable_names():
    """Noms des sources qui savent chercher dans un catalogue (search_url + result_regex, ou plugin)."""
    out = []
    for name in sorted(SOURCES):
        cls = SOURCES[name]
        try:
            if cls({**cls.defaults}).can_search():
                out.append(name)
        except Exception:
            pass
    return out


def load_plugins(plugins_dir, warn=None):
    """Charge les .py de plugins_dir. Retourne la liste des erreurs (str)."""
    errors = []
    if not os.path.isdir(plugins_dir):
        return errors
    for fn in sorted(os.listdir(plugins_dir)):
        if fn.endswith(".py") and not fn.startswith("_"):
            try:
                spec = importlib.util.spec_from_file_location(f"mangarr_plugin_{fn[:-3]}",
                                                              os.path.join(plugins_dir, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as e:  # un plugin cassé ne doit pas empêcher le reste
                msg = f"Plugin {fn} ignoré : {e}"
                errors.append(msg)
                if warn:
                    warn(msg)
    return errors


def load_custom_sources(repo):
    """Enregistre les sources définies par formulaire (kind = pattern | html)."""
    for row in repo.list_custom_sources():
        base = {"pattern": PatternSource, "html": HtmlSource}.get(row["kind"])
        if base is None or row["name"] in BUILTIN:
            continue
        cls = type(f"Custom_{row['name']}", (base,), {"defaults": dict(row["defaults"]),
                                                      "__doc__": row.get("description") or "source personnalisée"})
        register(row["name"])(cls)


def build_source(source_key, manga_params, settings):
    """(instance de source, params fusionnés avec les valeurs par défaut)."""
    if source_key not in SOURCES:
        raise ValueError(f"source inconnue '{source_key}' (disponibles : {', '.join(sorted(SOURCES))})")
    cls = SOURCES[source_key]
    params = {**cls.defaults, **manga_params}
    src = cls(params, page_delay=settings.page_delay)
    if isinstance(src, patreon.PatreonSource):
        src.export_dir = settings.patreon_dir
    return src, params
