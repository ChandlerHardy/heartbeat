"""Suite-wide hygiene.

2026-09-11: the pipeline tests had been appending a 200KB fake dossier to the
REAL ~/.worksweep/issues/pb-www/1775 on every run -- `dossier.root` falls
back to the home directory whenever a test config leaves `issues_root`
empty, and most test configs do. Point the fallback at a per-test directory
so no test can write outside its tmp_path, whatever config it builds.

2026-09-12: the same shape, read side. `models._gate_in_force` resolves the
domain gate lazily from the REAL domains.json in the ferdinand checkout, and
the result is module state that outlives the test that resolved it. Four
prompt tests (feedback + pipeline "names every gated path") passed in the
full suite only because test_domain_registry's teardown happened to run first
and leave the fallback behind; run alone, they read the live registry and
failed. Every test now starts on the baked-in fallback, and a test that wants
a registry calls refresh_domain_gate with its own file.
"""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_dossier_root(tmp_path, monkeypatch):
    from worksweep import dossier
    monkeypatch.setattr(dossier, "DEFAULT_ROOT", str(tmp_path / "_issues"))


@pytest.fixture(autouse=True)
def _hermetic_domain_gate(monkeypatch):
    from worksweep import models
    monkeypatch.setattr(models, "_active_gate",
                        (models.DOMAIN_GATE_PATHS, ()))
