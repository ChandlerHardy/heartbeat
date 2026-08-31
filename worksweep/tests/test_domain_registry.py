"""The domain gate's path list can come from the domain-check registry
(domains.json in the ferdinand checkout) instead of the baked-in constant.

Contract (spec: ferdinand-personal docs/specs/2026-08-31-domain-guard-design.md §4c):
  - a readable, valid registry supplies the gate patterns (gate-severity
    domains whose repos include pb-www, plus their path_exclusions);
  - anything less than that — missing file, unparseable JSON, invalid shape,
    or a registry that yields ZERO patterns for pb-www — falls back to the
    baked-in DOMAIN_GATE_PATHS, because an absent registry must never mean
    an absent gate (fail-closed);
  - the baked-in fallback is pinned byte-for-byte so it cannot silently weaken.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep import models  # noqa: E402
from worksweep.models import (DOMAIN_GATE_PATHS, refresh_domain_gate,  # noqa: E402
                              touches_domain_gate)


REGISTRY = {
    "version": 1,
    "domains": [
        {
            "id": "pla-db-mongo",
            "owner": {"name": "Leif Pedersen", "gitlab": "LeifPedersen"},
            "repos": ["pb-www"],
            "paths": ["phplib/local/DB/**", "phplib/local/*Mongo*", "db/**",
                      "**/migration*/**", "maintenance/mongodb/**",
                      "maintenance/*mongo*", "**/*.sql"],
            "path_exclusions": ["test/**"],
            "process": "issue first",
            "rules": [{"severity": "gate", "text": "no driver changes"}],
        },
        {
            "id": "pb-api-contracts",
            "owner": {"name": "Mike Dotson", "gitlab": "mgdotson"},
            "repos": ["pb-api"],
            "paths": ["**/*.go"],
            "process": "ask mike",
            "rules": [{"severity": "gate", "text": "contracts need mike"}],
        },
    ],
}


def write_registry(tmp_path, doc):
    p = tmp_path / "domains.json"
    p.write_text(json.dumps(doc))
    return str(p)


def teardown_function(_fn):
    # Every test leaves the module on the baked-in fallback, whatever it did.
    refresh_domain_gate(path=None)


def test_fallback_constant_is_pinned():
    # The fail-closed floor. Editing this list is a deliberate act that must
    # break a test, not a drive-by.
    assert DOMAIN_GATE_PATHS == ("phplib/local/DB/", "phplib/local/*Mongo*",
                                 "db/", "maintenance/mongodb/**",
                                 "maintenance/*mongo*")


def test_registry_supplies_the_gate(tmp_path):
    refresh_domain_gate(path=write_registry(tmp_path, REGISTRY))
    assert touches_domain_gate(["phplib/local/DB/Mongo.php"]) == (
        "phplib/local/DB/Mongo.php",)
    assert touches_domain_gate(["phplib/local/InventoryEventsMongo.php"]) == (
        "phplib/local/InventoryEventsMongo.php",)
    assert touches_domain_gate(["db/2026_add_index.php"]) == (
        "db/2026_add_index.php",)


def test_registry_adds_patterns_the_fallback_lacks(tmp_path):
    # **/migration*/** is registry-only today: with the registry loaded it
    # gates, on the fallback it does not. Both directions asserted so this
    # test documents (and notices) the difference instead of hiding it.
    nested = "phplib/local/migrations/2026_backfill.php"
    top = "migrations/2026_backfill.php"
    refresh_domain_gate(path=write_registry(tmp_path, REGISTRY))
    assert touches_domain_gate([nested]) == (nested,)
    assert touches_domain_gate([top]) == (top,)
    refresh_domain_gate(path=None)
    assert touches_domain_gate([nested]) == ()


def test_registry_exclusions_and_test_components_both_apply(tmp_path):
    refresh_domain_gate(path=write_registry(tmp_path, REGISTRY))
    # test/** is excluded by the registry AND by _TEST_COMPONENTS; a nested
    # tests/ dir is only covered by the component rule. Neither may gate.
    assert touches_domain_gate(["test/phpunit/unit/MongoTest.php"]) == ()
    assert touches_domain_gate(["phplib/local/DB/tests/MongoTest.php"]) == ()


def test_other_repos_domains_do_not_leak_in(tmp_path):
    refresh_domain_gate(path=write_registry(tmp_path, REGISTRY))
    # pb-api-contracts gates **/*.go — but only for pb-api. The pb-www sweep
    # must not gate a stray .go file on that domain's account.
    assert touches_domain_gate(["tools/helper.go"]) == ()


def test_missing_file_falls_back(tmp_path):
    refresh_domain_gate(path=str(tmp_path / "nope.json"))
    assert touches_domain_gate(["phplib/local/DB/Mongo.php"]) == (
        "phplib/local/DB/Mongo.php",)
    assert touches_domain_gate(["phplib/local/migrations/x.php"]) == ()


def test_corrupt_json_falls_back(tmp_path):
    p = tmp_path / "domains.json"
    p.write_text("{not json")
    refresh_domain_gate(path=str(p))
    assert touches_domain_gate(["phplib/local/DB/Mongo.php"]) == (
        "phplib/local/DB/Mongo.php",)


def test_registry_with_no_pbwww_gate_domain_falls_back(tmp_path):
    # A valid registry that yields zero patterns for pb-www is treated as no
    # registry at all: an empty gate is weaker than the fallback and must not
    # win silently.
    doc = {"version": 1, "domains": [REGISTRY["domains"][1]]}
    refresh_domain_gate(path=write_registry(tmp_path, doc))
    assert touches_domain_gate(["phplib/local/DB/Mongo.php"]) == (
        "phplib/local/DB/Mongo.php",)


def test_advisory_only_domain_contributes_nothing(tmp_path):
    # Only gate-severity domains harden the push gate; a domain whose rules
    # are all advisory is review guidance, not a push blocker.
    doc = json.loads(json.dumps(REGISTRY))
    doc["domains"][0]["rules"] = [{"severity": "advisory", "text": "tidy"}]
    refresh_domain_gate(path=write_registry(tmp_path, doc))
    # With the only pb-www domain advisory-only, the loader yields zero
    # patterns -> fail-closed fallback keeps gating DB/ but not migrations.
    assert touches_domain_gate(["phplib/local/DB/Mongo.php"]) == (
        "phplib/local/DB/Mongo.php",)
    assert touches_domain_gate(["phplib/local/migrations/x.php"]) == ()


def test_sql_gates_from_registry_and_fallback(tmp_path):
    for path_arg in (write_registry(tmp_path, REGISTRY), None):
        refresh_domain_gate(path=path_arg)
        assert touches_domain_gate(["www/reports/schema_tweak.sql"]) == (
            "www/reports/schema_tweak.sql",), path_arg


def test_gate_text_reflects_active_source(tmp_path):
    refresh_domain_gate(path=write_registry(tmp_path, REGISTRY))
    assert "migration" in models.domain_gate_text()
    refresh_domain_gate(path=None)
    assert "migration" not in models.domain_gate_text()


def test_config_parses_domain_registry_path(tmp_path):
    from worksweep.config import load_config
    cfg_file = tmp_path / "heartbeat.json"
    cfg_file.write_text(json.dumps({
        "repos": ["performancelivestock/pb-www"],
        "username": "chandler.hardy",
        "discord_webhook": "https://example.invalid/hook",
        "runner": {"domain_registry_path": "/opt/domains.json"},
    }))
    assert load_config(str(cfg_file)).domain_registry_path == "/opt/domains.json"
    cfg_file.write_text(json.dumps({
        "repos": ["performancelivestock/pb-www"],
        "username": "chandler.hardy",
        "discord_webhook": "https://example.invalid/hook",
    }))
    assert load_config(str(cfg_file)).domain_registry_path == ""


def test_main_refreshes_gate_after_config_load():
    # Wiring pin: the entrypoint must resolve the gate from the configured
    # registry before any executor runs. Source anchor (the module is
    # argparse-driven; executing main() would need a full queue harness for
    # one line of wiring).
    import inspect
    from worksweep import __main__ as entry
    src = inspect.getsource(entry.main)
    call = src.find("models.refresh_domain_gate(cfg.domain_registry_path")
    first_dispatch = src.find('if args.command')
    assert call != -1, "main() must activate the configured domain registry"
    assert call < first_dispatch, "gate resolution must precede command dispatch"
