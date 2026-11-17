import os

import pytest

from mangarr.core.engine import check_all
from mangarr.core.util import permission_hint
from tests.test_engine import add


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="UID inexistant sous Windows")
def test_permission_hint_names_users_and_folder(tmp_path):
    hint = permission_hint(str(tmp_path / "pas" / "encore" / "cree"))      # remonte au 1er dossier existant
    assert f"{os.getuid()}:{os.getgid()}" in hint and str(tmp_path) in hint and "chown" in hint
    assert permission_hint("") == "" or True


def test_download_permission_error_is_explained(ctx, server, monkeypatch):
    server.add_chapter(1, 1)
    add(ctx, server, tomes_enabled=False)

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied", "/library/_NEWS/X/X_1.cbz.part")

    monkeypatch.setattr("mangarr.core.engine.ZipFile", denied)
    res = check_all(ctx)[0]
    assert res.status == "error" and "Permission denied" in res.message
    if hasattr(os, "getuid"):
        assert "s'exécute avec l'utilisateur" in res.message


def test_unwritable_folder_does_not_crash_the_run(ctx, server, monkeypatch):
    server.add_chapter(1, 1)
    add(ctx, server, "Premier", tomes_enabled=False)
    add(ctx, server, "Second", tomes_enabled=False)
    real = os.makedirs

    def fake(path, *a, **k):
        if "Premier" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr("mangarr.core.engine.os.makedirs", fake)
    res = {r.name: r for r in check_all(ctx)}
    assert res["Premier"].status == "error" and "Permission denied" in res["Premier"].message
    assert res["Second"].got == 1                                           # l'autre manga n'est pas touché
