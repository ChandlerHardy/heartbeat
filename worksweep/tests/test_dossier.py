"""Per-issue dossier (2026-09-09): the durable context every lane reads and
writes, plus the session id that lets a later lane resume the conversation."""
import json
import os

from worksweep import dossier
from worksweep.config import WorksweepConfig


def _cfg(tmp_path):
    return WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="",
                           issues_root=str(tmp_path / "issues"))


def test_record_appends_titled_sections_and_read_returns_them(tmp_path):
    cfg = _cfg(tmp_path)
    assert dossier.record(cfg, "pb-www", 1830, "Implement", "MR !4200 on fix/1830-x, dev2.")
    assert dossier.record(cfg, "pb-www", 1830, "Feedback round", "addressed 2, replied 1.")
    text = dossier.read(cfg, "pb-www", 1830)
    assert text.startswith("# pb-www #1830 — dossier")
    assert "## Implement — 20" in text and "## Feedback round — 20" in text
    assert text.index("MR !4200") < text.index("addressed 2")
    assert os.path.isfile(tmp_path / "issues" / "pb-www" / "1830" / "dossier.md")


def test_read_is_empty_for_an_unknown_issue_and_trims_the_head_over_the_cap(tmp_path):
    cfg = _cfg(tmp_path)
    assert dossier.read(cfg, "pb-www", 999) == ""
    for i in range(40):
        dossier.record(cfg, "pb-www", 1, f"Section {i}", "x" * 500)
    text = dossier.read(cfg, "pb-www", 1, cap=4000)
    assert text.startswith("(earlier sections trimmed)\n## Section")
    assert "Section 39" in text and "Section 0 —" not in text
    assert len(text.encode("utf-8")) <= 4100


def test_mr_link_round_trips_and_is_none_when_absent(tmp_path):
    cfg = _cfg(tmp_path)
    assert dossier.issue_for_mr(cfg, "pb-www", 4200) is None
    assert dossier.link_mr(cfg, "pb-www", 4200, 1830)
    assert dossier.issue_for_mr(cfg, "pb-www", 4200) == 1830


def test_session_round_trips_and_expires(tmp_path):
    cfg = _cfg(tmp_path)
    assert dossier.load_session(cfg, "pb-www", 1830) is None
    assert dossier.save_session(cfg, "pb-www", 1830, "sess-abc", "implement")
    assert dossier.load_session(cfg, "pb-www", 1830) == "sess-abc"
    # eight days later it is too old to trust
    stale = json.load(open(tmp_path / "issues" / "pb-www" / "1830" / "session.json"))
    assert stale["lane"] == "implement"
    assert dossier.load_session(cfg, "pb-www", 1830,
                                now="2036-01-01T00:00:00+00:00") is None
    assert not dossier.save_session(cfg, "pb-www", 1830, "", "feedback")


def test_prompt_block_frames_the_dossier_as_history_not_truth():
    assert dossier.prompt_block("") == ""
    block = dossier.prompt_block("## Implement — t\nchose discard over 403")
    assert block.startswith("ISSUE DOSSIER")
    assert "history, not truth" in block and "chose discard over 403" in block
    assert "history, not truth" in dossier.RESUME_PREAMBLE


def test_dossier_failures_never_raise(tmp_path, capsys):
    cfg = WorksweepConfig(repos=("pb-www",), username="me", discord_webhook="",
                          issues_root=str(tmp_path / "blocked"))
    (tmp_path / "blocked").write_text("a file, not a dir")
    assert dossier.record(cfg, "pb-www", 1, "t", "b") is False
    assert dossier.link_mr(cfg, "pb-www", 2, 1) is False
    assert dossier.save_session(cfg, "pb-www", 1, "s", "implement") is False
    assert dossier.read(cfg, "pb-www", 1) == ""
    assert "dossier record failed" in capsys.readouterr().err
