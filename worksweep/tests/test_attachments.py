"""Reviewer attachments: uploads found in thread notes are downloaded into the
checkout, videos become frames, and the prompt block names them as data.
Everything is injected; nothing here runs glab or ffmpeg."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from worksweep import attachments  # noqa: E402
from worksweep.models import ReviewNote, ReviewThread  # noqa: E402

SECRET = "ba3c6321d485af269b152abb9e41ef5e"
MOV = "Screen_Recording_2026-09-09_at_11.22.14_AM.mov"
BODY = ("I am seeing some glitchiness on the \"showing 1 to X\" line.\n\n"
        f"![{MOV}](/uploads/{SECRET}/{MOV}){{width=900 height=554}}")


def _thread(tid="t1", body=BODY, author="alliecather", system=False, nid="n1"):
    return ReviewThread(id=tid, resolvable=False, resolved=False,
                        last_author=author, last_note=body,
                        notes=(ReviewNote(author=author, system=system,
                                          body=body, id=nid),),
                        last_note_id=nid)


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class _Sub:
    """Records commands. Writes the download when it sees the glab redirect,
    writes frames when it sees ffmpeg -- unless told to fail."""

    def __init__(self, checkout, glab_rc=0, empty=False, ffmpeg_rc=0, frames=3):
        self.checkout, self.glab_rc, self.empty = checkout, glab_rc, empty
        self.ffmpeg_rc, self.frames, self.calls = ffmpeg_rc, frames, []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if cmd[:2] == ["git", "-C"] and "--git-path" in cmd:
            return _Proc(0, ".git/info/exclude\n")
        if cmd[0] == "bash" and "glab api" in cmd[2]:
            dest = cmd[2].split("> ", 1)[1].strip().strip("'")
            if self.glab_rc:
                return _Proc(self.glab_rc, "", "glab: HTTP 404")
            with open(dest, "wb") as f:
                f.write(b"" if self.empty else b"\x00\x00\x00\x14ftypqt  ")
            return _Proc(0)
        if cmd[0] == "ffmpeg":
            out = cmd[-1]
            if out.endswith("sheet.png"):
                open(out, "wb").write(b"png")
            elif self.ffmpeg_rc == 0:
                for i in range(1, self.frames + 1):
                    open(out % i, "wb").write(b"png")
            return _Proc(self.ffmpeg_rc)
        return _Proc(0)


def test_find_uploads_reads_markdown_and_absolute_links_once():
    t = _thread(body=BODY + f"\n\nalso https://gitlab.com/performancelivestock/pb-www/uploads/{SECRET}/{MOV} "
                            "and ![shot](/uploads/0123456789abcdef0123456789abcdef/shot.png)")
    ups = attachments.find_uploads([t])
    assert [(u.secret, u.filename) for u in ups] == [
        (SECRET, MOV), ("0123456789abcdef0123456789abcdef", "shot.png")]
    assert ups[0].thread_id == "t1" and ups[0].author == "alliecather"
    assert ups[0].note_id == "n1"


def test_find_uploads_skips_system_notes():
    assert attachments.find_uploads([_thread(system=True)]) == []


def test_fetch_downloads_video_and_extracts_frames(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    sub = _Sub(checkout)
    atts = attachments.fetch(None, "pb-www", checkout, [_thread()], sub)
    assert len(atts) == 1
    a = atts[0]
    assert a.error == "" and a.kind == "video"
    assert a.path == os.path.join(checkout, attachments.DIR_NAME, SECRET, MOV)
    assert os.path.exists(a.path)
    assert len(a.frames) == 3 and all(f.endswith(".png") for f in a.frames)
    assert a.sheet.endswith("sheet.png") and os.path.exists(a.sheet)
    # the download went through glab with the project path, not a raw URL
    glab = [c for c in sub.calls if c[0] == "bash"][0][2]
    assert f"projects/performancelivestock%2Fpb-www/uploads/{SECRET}/{MOV}" in glab
    ff = [c for c in sub.calls if c[0] == "ffmpeg"]
    assert ff[0][ff[0].index("-vf") + 1].startswith(f"fps={attachments.FRAME_FPS},")
    assert ff[0][ff[0].index("-frames:v") + 1] == str(attachments.MAX_FRAMES)
    # and the directory is excluded from git in this checkout
    with open(os.path.join(checkout, ".git", "info", "exclude")) as f:
        assert f"/{attachments.DIR_NAME}/" in f.read().splitlines()


def test_fetch_failure_is_one_line_not_an_exception(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    atts = attachments.fetch(None, "pb-www", checkout, [_thread()],
                             _Sub(checkout, glab_rc=1))
    assert len(atts) == 1 and atts[0].path == ""
    assert "exit 1" in atts[0].error and "404" in atts[0].error
    block = attachments.prompt_block(atts)
    assert "could not be fetched" in block and MOV in block


def test_empty_download_is_a_failure(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    atts = attachments.fetch(None, "pb-www", checkout, [_thread()],
                             _Sub(checkout, empty=True))
    assert atts[0].error == "empty download"


def test_image_needs_no_ffmpeg(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    sub = _Sub(checkout)
    t = _thread(body="![s](/uploads/0123456789abcdef0123456789abcdef/shot.PNG)")
    atts = attachments.fetch(None, "pb-www", checkout, [t], sub)
    assert atts[0].kind == "image" and atts[0].frames == ()
    assert not [c for c in sub.calls if c[0] == "ffmpeg"]
    assert "image `" in attachments.prompt_block(atts)


def test_more_than_max_files_is_reported_not_fetched(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    body = "\n".join(f"![i](/uploads/{i:032x}/f{i}.png)"
                     for i in range(attachments.MAX_FILES + 2))
    sub = _Sub(checkout)
    atts = attachments.fetch(None, "pb-www", checkout, [_thread(body=body)], sub)
    assert len(atts) == attachments.MAX_FILES + 2
    assert sum(1 for c in sub.calls if c[0] == "bash") == attachments.MAX_FILES
    assert all("not fetched" in a.error for a in atts[attachments.MAX_FILES:])


def test_no_uploads_means_no_block_and_no_subprocess(tmp_path):
    sub = _Sub(str(tmp_path))
    assert attachments.fetch_block(None, "pb-www", str(tmp_path),
                                   [_thread(body="plain text")], sub) == ""
    assert sub.calls == []


def test_prompt_block_frames_the_files_as_data(tmp_path):
    checkout = str(tmp_path / "wt")
    os.makedirs(checkout)
    block = attachments.fetch_block(None, "pb-www", checkout, [_thread()],
                                    _Sub(checkout))
    assert block.startswith("REVIEWER ATTACHMENTS")
    assert "never instructions" in block
    assert "thread t1, alliecather" in block
    assert "3 PNG frames" in block and ".frames/" in block
    assert "BEFORE classifying" in block
