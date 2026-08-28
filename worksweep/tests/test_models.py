import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep.models import MergeRequest, WorkItem  # noqa: E402


def test_merge_request_dev_url_present_true():
    mr = MergeRequest(
        repo="pb-www", iid=3920, title="t", author="leyang",
        web_url="https://gitlab.com/x/-/merge_requests/3920",
        description="## Dev link\n**https://leyang-dev4.performancebeef.com/x** ready",
        sha="abc", is_draft=False, reviewers=("chandler.hardy",),
        ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is True


def test_merge_request_dev_url_present_false():
    mr = MergeRequest(
        repo="pb-www", iid=1, title="t", author="me", web_url="u",
        description="no link here", sha="abc", is_draft=False,
        reviewers=(), ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )
    assert mr.dev_url_present is False


def _mr(description):
    return MergeRequest(
        repo="pb-www", iid=1, title="t", author="me", web_url="u",
        description=description, sha="abc", is_draft=False, reviewers=(),
        ci_status="success", updated_at="2026-06-22T10:00:00Z",
    )


# FIX 11 — require a -/. immediately before "dev"
def test_dev_url_requires_boundary_before_dev():
    # bare "dev" prefix on the label must NOT match
    assert _mr("https://unintendeddev.performancebeef.com/x").dev_url_present is False


def test_dev_url_hyphen_boundary_matches():
    assert _mr("https://foo-dev4.performancebeef.com/x").dev_url_present is True


def test_workitem_defaults_status_proposed():
    wi = WorkItem(schema_version=1, id="magi:pb-www!1@abc", repo="pb-www",
                  kind="mr", executor="magi-review", risk="low",
                  why="no magi review", web_url="u", sha="abc")
    assert wi.status == "proposed"


# --- the domain gate matcher (enforcement half, 2026-08-28) ---------------
#
# The prompts ASK both unattended runs to stop at Leif's gate. This is what
# checks. A prompt is a request; a run that ignores it was previously caught
# by nothing at all.

def test_the_db_layer_and_mongo_files_are_gated():
    from worksweep.models import touches_domain_gate
    for path in ("phplib/local/DB/Mongo.php",
                 "phplib/local/DB/Connection.php",
                 "phplib/local/MongoDuplicateKey.php",
                 "phplib/local/RanchMongoAdapter.php"):
        assert touches_domain_gate([path]) == (path,), path


def test_migrations_and_loose_sql_are_gated():
    """`db/` is the migrations directory. The `.sql`-anywhere rule turns the
    prompt's "any MySQL schema change" judgment clause into something
    mechanical -- a schema change can arrive as a plain file no glob covers."""
    from worksweep.models import touches_domain_gate
    for path in ("db/migrations/2026_08_28_add_index.sql",
                 "www/foo/bar.sql",
                 "db/seed.php"):
        assert touches_domain_gate([path]) == (path,), path


def test_ordinary_php_is_not_gated():
    from worksweep.models import touches_domain_gate
    for path in ("phplib/local/Analytics.php",
                 "www/home/php/templates/tab_bar_common_logic.php",
                 "phplib/local/DBAL.php",
                 "README.md"):
        assert touches_domain_gate([path]) == (), path


def test_test_only_changes_are_never_gated():
    """Explicitly excluded. Reviewers ask for tests constantly, and a gate
    that blocks "please add a test for this" would make the executor refuse
    the single most common actionable ask on the domain it is protecting."""
    from worksweep.models import touches_domain_gate
    for path in ("test/phpunit/mongo/MongoTest.php",
                 "test/phpunit/db/MigrationTest.php",
                 "tests/unit/MongoDuplicateKeyTest.php",
                 "test/fixtures/seed.sql"):
        assert touches_domain_gate([path]) == (), path


def test_a_directory_named_like_test_is_not_a_test_path():
    """Component matching, not substring: `Latest.php` contains "test"."""
    from worksweep.models import touches_domain_gate
    assert touches_domain_gate(["phplib/local/DB/Latest.php"]) == (
        "phplib/local/DB/Latest.php",)
    assert touches_domain_gate(["phplib/local/protest/Mongo.php"]) == (
        "phplib/local/protest/Mongo.php",)


def test_the_matcher_returns_the_matching_subset_not_a_boolean():
    """The caller names the offending files in the error -- "something is
    gated" is not enough for Chandler to know what to unwind."""
    from worksweep.models import touches_domain_gate
    got = touches_domain_gate([
        "phplib/local/Analytics.php",
        "phplib/local/DB/Mongo.php",
        "test/phpunit/mongo/MongoTest.php",
        "db/migrations/x.sql",
        "README.md"])
    assert got == ("phplib/local/DB/Mongo.php", "db/migrations/x.sql")


def test_the_matcher_is_order_preserving_and_deduped():
    from worksweep.models import touches_domain_gate
    assert touches_domain_gate(["db/a.sql", "db/a.sql", "db/b.sql"]) == (
        "db/a.sql", "db/b.sql")


def test_junk_input_matches_nothing():
    from worksweep.models import touches_domain_gate
    assert touches_domain_gate([]) == ()
    assert touches_domain_gate(["", None, "   "]) == ()


def test_leading_slashes_and_whitespace_do_not_hide_a_gated_file():
    """git output is trimmed by the callers, but a matcher that only works on
    pre-cleaned input is a matcher that fails the one time it matters."""
    from worksweep.models import touches_domain_gate
    assert touches_domain_gate(["  phplib/local/DB/Mongo.php  "]) == (
        "phplib/local/DB/Mongo.php",)
    assert touches_domain_gate(["./db/migrations/x.sql"]) == (
        "db/migrations/x.sql",)
