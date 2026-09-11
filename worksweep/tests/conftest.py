"""Suite-wide hygiene.

2026-09-11: the pipeline tests had been appending a 200KB fake dossier to the
REAL ~/.worksweep/issues/pb-www/1775 on every run -- `dossier.root` falls
back to the home directory whenever a test config leaves `issues_root`
empty, and most test configs do. Point the fallback at a per-test directory
so no test can write outside its tmp_path, whatever config it builds.
"""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_dossier_root(tmp_path, monkeypatch):
    from worksweep import dossier
    monkeypatch.setattr(dossier, "DEFAULT_ROOT", str(tmp_path / "_issues"))
