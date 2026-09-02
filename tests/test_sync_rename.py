"""A re-worded headline renames the note; its recording has to follow.

Audio is keyed on the note stem, so reprocessing a transcript whose headline
changes left the old mp3 orphaned in Attachments/ — and, worse, the new stem
did not exist, so download_audio's "already downloaded" short-circuit missed
and the whole recording was fetched again.
"""
import logging
import re
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from transcript_analyzer import sync
from transcript_analyzer.db import get_conn, record_sync
from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer


FOREIGN_NOTE = """---
source: granola
date: 2026-07-01
transcript_id: someone-else
---

# Not ours
"""


def _pocket_cfg(cfg):
    return replace(cfg, pocket=replace(cfg.pocket, api_key="pk_test", download_audio=True))


def _transcript() -> Transcript:
    return Transcript(
        id="t1",
        source="pocket",
        native_id="n1",
        title="Raw source title",
        date=date(2026, 7, 1),
        text="Angela: we should ship the pricing deck.",
    )


def _seed_previous_note(cfg, transcript, headline: str):
    """Write the note (and its recording) a previous sync would have left."""
    old_insight = Insight(headline=headline, summary="Old summary.")
    old_note = writer.write_note(cfg, transcript, old_insight, audio_name=None)
    old_audio = writer.audio_path_for(cfg, old_note)
    old_audio.parent.mkdir(parents=True, exist_ok=True)
    old_audio.write_bytes(b"ID3-fake-mp3-bytes")
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn,
            transcript.source,
            transcript.native_id,
            "oldhash",
            str(old_note),
            datetime.now(timezone.utc).isoformat(),
        )
    return old_note, old_audio


def test_audio_follows_the_note_and_is_not_re_downloaded(cfg, monkeypatch):
    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    old_note, old_audio = _seed_previous_note(cfg, transcript, "Old pricing chat")

    new_insight = Insight(headline="Pricing deck review with Angela", summary="New.")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)

    # The real download path runs; only the network lookup is stubbed, so a
    # missed short-circuit shows up as a call here.
    from transcript_analyzer.connectors import pocket_api

    fetched = []

    def fake_audio_url(self, rec_id):
        fetched.append(rec_id)
        return ""

    monkeypatch.setattr(pocket_api.PocketClient, "audio_url", fake_audio_url)

    result = sync.process_transcript(cfg, transcript, llm=None)

    new_note = writer.note_path_for(cfg, transcript, new_insight)
    assert result["note_path"] == str(new_note)
    assert new_note != old_note and new_note.exists()
    assert not old_note.exists()

    new_audio = writer.audio_path_for(cfg, new_note)
    assert new_audio.exists(), "the recording was left behind under the old stem"
    assert new_audio.read_bytes() == b"ID3-fake-mp3-bytes"
    assert not old_audio.exists(), "the old mp3 is orphaned in Attachments/"
    assert fetched == [], "the recording the vault already had was re-downloaded"

    # The note still embeds its player, now pointing at the moved file.
    assert f"![[{new_audio.name}]]" in new_note.read_text(encoding="utf-8")


def test_unchanged_headline_leaves_the_recording_alone(cfg, monkeypatch):
    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    insight = Insight(headline="Pricing deck review with Angela", summary="Same.")
    note, audio = _seed_previous_note(cfg, transcript, insight.headline)

    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: insight)
    from transcript_analyzer.connectors import pocket_api

    monkeypatch.setattr(
        pocket_api.PocketClient, "audio_url", lambda self, rec_id: ""
    )

    sync.process_transcript(cfg, transcript, llm=None)

    assert audio.exists()
    assert audio.read_bytes() == b"ID3-fake-mp3-bytes"
    assert note.exists()


def test_move_audio_with_note_replaces_a_stale_file_at_a_stem_we_own(cfg):
    """Both ends ours: the recording moves, and the stale file gives way."""
    transcript = _transcript()
    old_note = writer.write_note(cfg, transcript, Insight(headline="Old"))
    new_note = writer.write_note(cfg, transcript, Insight(headline="New"))
    old_audio = writer.audio_path_for(cfg, old_note)
    new_audio = writer.audio_path_for(cfg, new_note)
    old_audio.parent.mkdir(parents=True, exist_ok=True)
    old_audio.write_bytes(b"keep-me")
    new_audio.write_bytes(b"stale")

    assert writer.move_audio_with_note(cfg, old_note, new_note, transcript.id) == new_audio
    assert new_audio.read_bytes() == b"keep-me"
    assert not old_audio.exists()

    # Nothing to move: same note, or no recording on disk.
    assert writer.move_audio_with_note(cfg, new_note, new_note, transcript.id) is None
    assert writer.move_audio_with_note(cfg, old_note, new_note, transcript.id) is None


def test_a_recording_we_cannot_prove_is_ours_is_never_moved_off_its_stem(cfg, caplog):
    """The SOURCE needs the same proof as the destination.

    A stem another note took — while this transcript's recording was still
    downloading, or by a retitle pass on the live vault — holds THEIR mp3.
    Moving it leaves their note with a dangling embed and their recording gone.
    """
    transcript = _transcript()
    stranger_note = cfg.vault.insights_path / "2026-07-01 taken.md"
    stranger_note.write_text(FOREIGN_NOTE, encoding="utf-8")
    stranger_audio = writer.audio_path_for(cfg, stranger_note)
    stranger_audio.parent.mkdir(parents=True, exist_ok=True)
    stranger_audio.write_bytes(b"stranger-recording")
    ours = writer.write_note(cfg, transcript, Insight(headline="Ours"))

    with caplog.at_level(logging.WARNING, logger="transcript_analyzer.obsidian.writer"):
        moved = writer.move_audio_with_note(cfg, stranger_note, ours, transcript.id)

    assert moved is None
    assert stranger_audio.read_bytes() == b"stranger-recording"
    assert not writer.audio_path_for(cfg, ours).exists()
    assert str(stranger_audio) in caplog.text


def test_a_recording_at_a_stem_we_cannot_prove_is_ours_is_never_unlinked(cfg, caplog):
    """The owner renamed the note in Obsidian; the attachment stayed behind.

    Obsidian rewrites the embed but leaves the file, so that mp3 is live and
    still referenced while its old note stem is free. Nothing may unlink it on
    the strength of a stem alone — an mp3 carries no frontmatter, so only a
    note at that stem can claim it.
    """
    transcript = _transcript()
    old_note = writer.write_note(cfg, transcript, Insight(headline="Old"))
    target = cfg.vault.insights_path / "2026-07-01 pricing-deck-review.md"
    old_audio = writer.audio_path_for(cfg, old_note)
    target_audio = writer.audio_path_for(cfg, target)
    old_audio.parent.mkdir(parents=True, exist_ok=True)
    old_audio.write_bytes(b"our-recording")
    target_audio.write_bytes(b"owner-recording")

    with caplog.at_level(logging.WARNING, logger="transcript_analyzer.obsidian.writer"):
        moved = writer.move_audio_with_note(cfg, old_note, target, transcript.id)

    assert moved is None
    assert target_audio.read_bytes() == b"owner-recording"
    assert old_audio.read_bytes() == b"our-recording", "our own recording was lost"
    assert str(target_audio) in caplog.text


def test_a_recording_whose_note_is_unreadable_is_never_unlinked(cfg, monkeypatch):
    transcript = _transcript()
    old_note = writer.write_note(cfg, transcript, Insight(headline="Old"))
    target = writer.write_note(cfg, transcript, Insight(headline="Target"))
    old_audio = writer.audio_path_for(cfg, old_note)
    target_audio = writer.audio_path_for(cfg, target)
    old_audio.parent.mkdir(parents=True, exist_ok=True)
    old_audio.write_bytes(b"our-recording")
    target_audio.write_bytes(b"unknown-recording")

    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if Path(self) == target:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)

    assert writer.move_audio_with_note(cfg, old_note, target, transcript.id) is None
    assert target_audio.read_bytes() == b"unknown-recording"
    assert old_audio.read_bytes() == b"our-recording"


def test_a_path_taken_during_the_download_keeps_the_body_and_the_disk_in_step(
    cfg, monkeypatch
):
    """One claim, threaded through the audio destination and the note.

    The claim is filesystem-dependent and the download between the two uses of
    it can run for minutes. Re-deciding afterwards put the note on the
    disambiguated stem while its '![[…]]' embed pointed at the original one, so
    the player silently disappeared from that note. The re-claim still has to
    happen — and the stem's recording now belongs to whoever took the stem, so
    it stays put and this note is written with no embed at all.
    """
    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    insight = Insight(headline="Pricing deck review with Angela", summary="S.")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: insight)

    claimed = writer.note_path_for(cfg, transcript, insight)

    def download_while_someone_takes_the_path(cfg_, transcript_, note_path):
        note_path.write_text(FOREIGN_NOTE, encoding="utf-8")
        audio = writer.audio_path_for(cfg_, note_path)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"stranger-recording")
        return audio.name, False

    monkeypatch.setattr(sync, "_maybe_download_audio", download_while_someone_takes_the_path)

    result = sync.process_transcript(cfg, transcript, llm=None)
    note_path = Path(result["note_path"])

    assert claimed.read_text(encoding="utf-8") == FOREIGN_NOTE, "wrote over a note that is not ours"
    assert note_path != claimed, "the re-claim did not happen"
    taken_audio = writer.audio_path_for(cfg, claimed)
    assert taken_audio.read_bytes() == b"stranger-recording", "took a recording that is not ours"

    # Every embed the note carries names a file that is actually there.
    body = note_path.read_text(encoding="utf-8")
    for name in re.findall(r"!\[\[([^\]]+)\]\]", body):
        assert (writer.attachments_dir(cfg) / name).exists(), name


def _granola_transcript() -> Transcript:
    return Transcript(
        id="g1",
        source="granola",
        native_id="gn1",
        title="Raw source title",
        date=date(2026, 7, 1),
        text="Angela: we should ship the pricing deck.",
    )


def _symlinked_vault_cfg(cfg, tmp_path):
    """A vault reached through a symlink: str(p) != str(p.resolve())."""
    real = tmp_path / "real_vault"
    (real / cfg.vault.insights_folder).mkdir(parents=True)
    link = tmp_path / "linked_vault"
    link.symlink_to(real, target_is_directory=True)
    return replace(cfg, vault=replace(cfg.vault, path=link))


def test_resync_keeps_the_note_when_sync_state_holds_the_resolved_path(
    cfg, tmp_path, monkeypatch
):
    """The migration scripts store the RESOLVED note path; sync stored the raw
    one and compared the two strings, so on a symlinked (or relative) vault the
    cleanup deleted the note it had just written — and the index went empty."""
    cfg = _symlinked_vault_cfg(cfg, tmp_path)
    transcript = _granola_transcript()
    insight = Insight(headline="Pricing deck review with Angela", summary="S.")

    note = writer.write_note(cfg, transcript, insight)
    assert str(note) != str(note.resolve()), "vault path is not symlinked in this test"
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn,
            transcript.source,
            transcript.native_id,
            "oldhash",
            str(note.resolve()),
            datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: insight)
    result = sync.process_transcript(cfg, transcript, llm=None)

    assert Path(result["note_path"]).exists(), "the note it just wrote was deleted"
    from transcript_analyzer.db import all_transcripts, get_sync_note_path

    with get_conn(cfg.db_path) as conn:
        recs = all_transcripts(conn)
        stored = get_sync_note_path(conn, transcript.source, transcript.native_id)
    assert [r.transcript_id for r in recs] == [transcript.id]
    # One spelling in the DB, the same one the migration scripts write.
    assert stored == str(note.resolve())


def test_resync_still_removes_a_genuinely_renamed_note(cfg, tmp_path, monkeypatch):
    """Failing safe on path spellings must not stop real stale-note cleanup."""
    cfg = _symlinked_vault_cfg(cfg, tmp_path)
    transcript = _granola_transcript()

    old_note = writer.write_note(cfg, transcript, Insight(headline="Old headline"))
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn,
            transcript.source,
            transcript.native_id,
            "oldhash",
            str(old_note.resolve()),
            datetime.now(timezone.utc).isoformat(),
        )

    new_insight = Insight(headline="Pricing deck review with Angela", summary="S.")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)
    result = sync.process_transcript(cfg, transcript, llm=None)

    assert Path(result["note_path"]).exists()
    assert Path(result["note_path"]).name != old_note.name
    assert not old_note.exists()


def _other_transcript() -> Transcript:
    """A DIFFERENT recording on the same date whose headline slugifies alike."""
    return Transcript(
        id="t2",
        source="pocket",
        native_id="n2",
        title="Another raw title",
        date=date(2026, 7, 1),
        text="Ben: the other recording entirely.",
    )


def test_resync_never_deletes_a_note_it_cannot_prove_is_its_own(cfg, monkeypatch, caplog):
    """sync_state remembers a PATH, not a claim on the file living there.

    The owner deletes A's note; a later transcript B whose headline slugifies
    the same way legitimately takes that filename; A re-syncs under a re-worded
    headline. Acting on the remembered path alone moved B's recording onto A's
    new stem and then deleted B's note — unrecoverably, since sync is
    hash-idempotent and never revisits B.
    """
    cfg = _pocket_cfg(cfg)
    a = _transcript()
    shared_headline = "Pricing deck review with Angela"
    a_note, a_audio = _seed_previous_note(cfg, a, shared_headline)

    a_note.unlink()  # the vault owner deletes it in Obsidian
    a_audio.unlink()

    b = _other_transcript()
    b_note = writer.write_note(cfg, b, Insight(headline=shared_headline, summary="B."))
    assert b_note == a_note, "B did not take the filename A used to hold"
    b_audio = writer.audio_path_for(cfg, b_note)
    b_audio.write_bytes(b"B-recording-bytes")
    b_bytes = b_note.read_bytes()

    monkeypatch.setattr(
        sync,
        "extract_insight",
        lambda *args, **kwargs: Insight(headline="Q4 pricing follow-up", summary="A."),
    )
    from transcript_analyzer.connectors import pocket_api

    monkeypatch.setattr(pocket_api.PocketClient, "audio_url", lambda self, rec_id: "")

    with caplog.at_level(logging.WARNING, logger="transcript_analyzer.sync"):
        result = sync.process_transcript(cfg, a, llm=None)

    assert b_note.exists(), "another recording's note was deleted"
    assert b_note.read_bytes() == b_bytes
    assert b_audio.exists() and b_audio.read_bytes() == b"B-recording-bytes", (
        "another recording's mp3 was moved onto this transcript's stem"
    )
    assert str(b_note) in caplog.text, "the skipped path was not named to the operator"

    # A still gets its own note, under its own name.
    new_note = Path(result["note_path"])
    assert new_note.exists() and new_note != b_note
    assert a_audio == b_audio  # A's old stem is the one B now owns


def test_resync_leaves_an_unreadable_previous_note_alone(cfg, monkeypatch, caplog):
    """Unproven for ANY reason — including a read that fails — means not ours."""
    transcript = _granola_transcript()
    old_note = writer.write_note(cfg, transcript, Insight(headline="Old headline"))
    with get_conn(cfg.db_path) as conn:
        record_sync(
            conn,
            transcript.source,
            transcript.native_id,
            "oldhash",
            str(old_note.resolve()),
            datetime.now(timezone.utc).isoformat(),
        )
    before = old_note.read_bytes()

    real_read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if Path(self) == old_note:
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.setattr(
        sync,
        "extract_insight",
        lambda *args, **kwargs: Insight(headline="Brand new headline", summary="S."),
    )

    with caplog.at_level(logging.WARNING, logger="transcript_analyzer.sync"):
        result = sync.process_transcript(cfg, transcript, llm=None)

    assert old_note.exists() and old_note.read_bytes() == before
    assert Path(result["note_path"]) != old_note
    assert str(old_note) in caplog.text


class _FakeStream:
    """Enough of httpx.stream's context manager to drive download_audio."""

    def __init__(self, chunks, on_open=None):
        self._chunks = chunks
        self._on_open = on_open

    def __enter__(self):
        if self._on_open is not None:
            self._on_open()
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=None):
        yield from self._chunks


def _stub_audio_download(monkeypatch, chunks, on_open=None):
    from transcript_analyzer.connectors import pocket_api

    monkeypatch.setattr(
        pocket_api.PocketClient, "audio_url", lambda self, rec_id: "https://x.invalid/a.mp3"
    )
    monkeypatch.setattr(
        pocket_api.httpx,
        "stream",
        lambda *args, **kwargs: _FakeStream(chunks, on_open=on_open),
    )
    return pocket_api


def test_a_finished_download_never_replaces_a_stem_taken_while_it_streamed(
    cfg, monkeypatch, caplog
):
    """The destination is proven free at the START of a multi-minute stream.

    A retitle pass on the live vault legitimately takes that stem meanwhile and
    moves ITS recording there; the unconditional replace at the end then
    destroyed a recording that was never ours, unrecoverably.
    """
    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    note = cfg.vault.insights_path / "2026-07-01 pricing-deck-review.md"
    dest = writer.audio_path_for(cfg, note)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def someone_takes_the_stem():
        note.write_text(FOREIGN_NOTE, encoding="utf-8")
        dest.write_bytes(b"stranger-recording")

    pocket_api = _stub_audio_download(
        monkeypatch, [b"our-audio"], on_open=someone_takes_the_stem
    )

    with caplog.at_level(
        logging.WARNING, logger="transcript_analyzer.connectors.pocket_api"
    ):
        with pocket_api.PocketClient(cfg) as pc:
            with pytest.raises(pocket_api.AudioStemTaken):
                pc.download_audio("rec1", dest, transcript.id)

    assert dest.read_bytes() == b"stranger-recording", "a stranger's recording was replaced"
    assert not writer.audio_partial(dest).exists(), "the discarded download was left behind"
    assert str(dest) in caplog.text


def test_a_download_onto_a_free_stem_still_lands(cfg, monkeypatch):
    """Re-proving the destination must not disable the ordinary download."""
    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    note = cfg.vault.insights_path / "2026-07-01 pricing-deck-review.md"
    dest = writer.audio_path_for(cfg, note)
    dest.parent.mkdir(parents=True, exist_ok=True)

    pocket_api = _stub_audio_download(monkeypatch, [b"our-", b"audio"])

    with pocket_api.PocketClient(cfg) as pc:
        got = pc.download_audio("rec1", dest, transcript.id)

    assert got == dest
    assert dest.read_bytes() == b"our-audio"
    assert not writer.audio_partial(dest).exists()


class _StubLLM:
    """The budget/kill-switch gate sync() consults before a pass."""

    def __init__(self, cfg):
        pass

    def health(self):
        return {
            "ok": True,
            "kill_switch": False,
            "key_configured": True,
            "month_spend_usd": 0.0,
            "monthly_budget_usd": 5.0,
        }


def test_a_discarded_download_is_fetched_again_on_the_next_sync(cfg, monkeypatch):
    """A discard is work still owed, not work done.

    process_transcript records the transcript's hash and sync() skips anything
    whose stored hash matches, so remembering a discarded recording as success
    left the note without its player until the upstream text changed.
    """
    cfg = _pocket_cfg(cfg)
    transcript = Transcript(
        id="t1",
        source="pocket",
        native_id="n1",
        title="Raw source title",
        date=date(2026, 7, 1),
        text="Angela: we should ship the pricing deck this quarter. " * 40,
    )
    insight = Insight(headline="Pricing deck review with Angela", summary="S.")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: insight)
    monkeypatch.setattr(sync, "_iter_source", lambda *a, **k: iter([transcript]))
    monkeypatch.setattr(sync, "LLM", _StubLLM)

    from transcript_analyzer.connectors import pocket_api

    fetched = []

    def discard_then_succeed(self, rec_id, dest, transcript_id):
        fetched.append(rec_id)
        if len(fetched) == 1:
            raise pocket_api.AudioStemTaken(str(dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"our-recording")
        return dest

    monkeypatch.setattr(pocket_api.PocketClient, "download_audio", discard_then_succeed)

    def run():
        return sync.sync(cfg, sources=["pocket"], synthesize_after=False, verbose=False)

    first = run()
    assert first["processed"] == 1 and first["skipped"] == 0
    note = Path(first["items"][0]["note_path"])
    assert "![[" not in note.read_text(encoding="utf-8"), "embedded a discarded recording"

    second = run()
    assert second["processed"] == 1, "the discarded recording was never fetched again"
    assert fetched == ["n1", "n1"]
    note = Path(second["items"][0]["note_path"])
    audio = writer.audio_path_for(cfg, note)
    assert audio.read_bytes() == b"our-recording"
    assert f"![[{audio.name}]]" in note.read_text(encoding="utf-8")

    # Nothing owed now: an unchanged transcript still short-circuits.
    third = run()
    assert third["processed"] == 0 and third["skipped"] == 1
    assert fetched == ["n1", "n1"], "a settled recording was fetched again"


def test_study_notes_follow_a_retitle(cfg, monkeypatch):
    """A lecture's study notes and PDF are keyed on the note stem too, so a
    re-worded headline has to carry them across or the note links at nothing."""
    from transcript_analyzer.models import Insight

    transcript = _transcript()
    old_note = writer.write_note(
        cfg, transcript, Insight(headline="Old lecture name", summary="Old.")
    )
    study = writer.write_study_note(cfg, old_note, transcript.id, "notes")
    writer.write_study_pdf(study, transcript.id, b"%PDF ours")
    with get_conn(cfg.db_path) as conn:
        record_sync(conn, transcript.source, transcript.native_id, "oldhash",
                    str(old_note), datetime.now(timezone.utc).isoformat())

    new_insight = Insight(headline="Row reducing a 3x3 matrix", summary="New.")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)
    res = sync.process_transcript(cfg, transcript, llm=None)

    new_note = Path(res["note_path"])
    moved = writer.study_note_path_for(cfg, new_note)
    assert moved.exists() and not study.exists()
    assert writer.study_pdf_for(moved).read_bytes() == b"%PDF ours"


def test_a_retitle_carries_the_owners_content_onto_the_new_note(cfg, monkeypatch):
    """A rename writes to an empty stem, then deletes the old note.

    Everything the owner added lives at the OLD stem until that delete, so a
    regeneration that only ever looked at the destination found nothing and
    the rename silently destroyed the hand-typed tail, reopened every ticked
    commitment, and dropped the study link the same rename had just moved.
    """
    transcript = _transcript()
    old_note = writer.write_note(
        cfg, transcript,
        Insight(headline="Old lecture name", summary="Old.",
                action_items=["Send the deck", "Book the follow-up"]),
    )
    study = writer.write_study_note(cfg, old_note, transcript.id, "notes")
    writer.write_study_pdf(study, transcript.id, b"%PDF ours")

    # The owner ticks a commitment and types their own notes below the marker.
    text = old_note.read_text(encoding="utf-8").replace(
        "- [ ] Send the deck", "- [x] Send the deck"
    )
    old_note.write_text(
        text + "\n## My own notes\nAngela sounded unconvinced.\n", encoding="utf-8"
    )
    with get_conn(cfg.db_path) as conn:
        record_sync(conn, transcript.source, transcript.native_id, "oldhash",
                    str(old_note), datetime.now(timezone.utc).isoformat())

    new_insight = Insight(
        headline="Row reducing a 3x3 matrix", summary="New.",
        action_items=["Send the deck", "Book the follow-up"],
    )
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)
    res = sync.process_transcript(cfg, transcript, llm=None)

    new_note = Path(res["note_path"])
    assert new_note != old_note and not old_note.exists()
    body = new_note.read_text(encoding="utf-8")

    assert "Angela sounded unconvinced." in body, "the owner's tail was destroyed"
    assert "- [x] Send the deck" in body, "a closed commitment was reopened"
    assert "- [ ] Book the follow-up" in body
    moved = writer.study_note_path_for(cfg, new_note)
    assert f"[[{moved.stem}|Full study notes]]" in body
    assert f"[[{moved.stem}.pdf|Printable PDF]]" in body
    assert f'study_notes: "{moved.stem}"' in body


def test_a_previous_note_that_is_not_ours_carries_nothing(cfg, monkeypatch):
    """`previous` is a hint, never a permission: owns_note is re-proven."""
    transcript = _transcript()
    stranger = cfg.vault.insights_path / "2026-07-01 stranger.md"
    stranger.write_text(
        FOREIGN_NOTE + "\n## Action Items\n- [x] Their closed item\n"
        + writer.NOTE_END + "\nTheir private notes.\n",
        encoding="utf-8",
    )

    written = writer.write_note(
        cfg, transcript, Insight(headline="Ours", summary="s."),
        previous=stranger,
    )

    body = written.read_text(encoding="utf-8")
    assert "Their private notes." not in body
    assert "Their closed item" not in body
    assert "Their private notes." in stranger.read_text(encoding="utf-8")


def test_a_failed_lecture_pass_strands_no_recording_and_never_ladders(cfg, monkeypatch):
    """A propagating study-notes failure must leave no half-state.

    The download used to run first, so a truncated lecture response left an
    mp3 at a stem no note ever occupied. `claimable_stem` then refused that
    stem forever, so every retry landed one rung up the ladder and fetched the
    whole recording again.
    """
    from transcript_analyzer.pipeline import lecture as lecture_mod
    from transcript_analyzer.pipeline.llm import LLMResponseError

    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    insight = Insight(headline="Row reducing a 3x3 matrix", summary="s.", kind="lecture")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: insight)

    downloads = []

    def record_download(cfg_, t, note_path):
        downloads.append(note_path)
        audio = writer.audio_path_for(cfg_, note_path)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"our-recording")
        return audio.name, False

    monkeypatch.setattr(sync, "_maybe_download_audio", record_download)

    truncated = True

    def produce(*a, **k):
        if truncated:
            raise LLMResponseError("Structured output truncated at max_tokens")
        raise AssertionError("unreachable")

    monkeypatch.setattr(lecture_mod, "produce", produce)

    with pytest.raises(LLMResponseError):
        sync.process_transcript(cfg, transcript, llm=None)

    assert downloads == [], "a recording was fetched before the pass that failed"
    attachments = cfg.vault.insights_path / writer.ATTACHMENTS_SUBDIR
    assert not list(attachments.glob("*.mp3")), "an mp3 was stranded at an unclaimed stem"

    # The retry reuses the SAME stem rather than laddering past a poisoned one.
    truncated = False
    monkeypatch.setattr(sync, "_study_notes_for", lambda *a, **k: (None, ""))
    res = sync.process_transcript(cfg, transcript, llm=None)
    note = Path(res["note_path"])
    assert note == writer.note_path_for(cfg, transcript, insight)
    assert "(t1" not in note.stem, f"the claim ladder advanced: {note.name}"
    assert downloads == [note]


def test_a_propagating_lecture_pass_leaves_the_rename_untouched(cfg, monkeypatch):
    """Every mutating step runs after every step that can propagate.

    The moves used to run first, so a lecture pass that raised left the mp3
    and the study notes on the destination stem with no note there.
    `claimable_stem` then refused that stem forever, the next cycle laddered
    one rung up, and the whole recording was fetched again — every cycle.
    """
    from transcript_analyzer.pipeline import lecture as lecture_mod
    from transcript_analyzer.pipeline.llm import LLMResponseError

    cfg = _pocket_cfg(cfg)
    transcript = _transcript()
    old_note, old_audio = _seed_previous_note(cfg, transcript, "Old lecture name")
    old_study = writer.write_study_note(cfg, old_note, transcript.id, "notes")
    writer.write_study_pdf(old_study, transcript.id, b"%PDF ours")

    new_insight = Insight(headline="Row reducing a 3x3 matrix", summary="New.",
                          kind="lecture")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)

    failing = True

    def produce(*a, **k):
        if failing:
            raise LLMResponseError("Claude returned invalid JSON")
        raise AssertionError("unreachable")

    monkeypatch.setattr(lecture_mod, "produce", produce)

    before = {p: p.read_bytes() for p in cfg.vault.insights_path.rglob("*")
              if p.is_file()}
    with pytest.raises(LLMResponseError):
        sync.process_transcript(cfg, transcript, llm=None)

    after = {p: p.read_bytes() for p in cfg.vault.insights_path.rglob("*")
             if p.is_file()}
    assert after == before, "a failing lecture pass mutated the vault"
    assert old_note.exists() and old_audio.exists() and old_study.exists()

    # The retry reuses the SAME stem: nothing poisoned it.
    failing = False
    monkeypatch.setattr(sync, "_study_notes_for", lambda *a, **k: (None, ""))
    res = sync.process_transcript(cfg, transcript, llm=None)
    new_note = Path(res["note_path"])
    assert new_note == writer.note_path_for(cfg, transcript, new_insight)
    assert "(t1" not in new_note.stem, f"the claim ladder advanced: {new_note.name}"
    assert writer.audio_path_for(cfg, new_note).read_bytes() == b"ID3-fake-mp3-bytes"
    assert not old_audio.exists()


def test_fresh_study_notes_are_not_clobbered_by_the_rename_move(cfg, monkeypatch):
    """The pass now claims the destination stem before the move would run."""
    from transcript_analyzer.pipeline import lecture as lecture_mod

    transcript = _transcript()
    old_note, _old_audio = _seed_previous_note(cfg, transcript, "Old lecture name")
    old_study = writer.write_study_note(cfg, old_note, transcript.id, "stale notes")

    new_insight = Insight(headline="Row reducing a 3x3 matrix", summary="New.",
                          kind="lecture")
    monkeypatch.setattr(sync, "extract_insight", lambda *a, **k: new_insight)

    def produce(cfg_, t, ins, note_path, llm=None, **k):
        from transcript_analyzer.models import StudyNotes

        study_path = writer.write_study_note(cfg_, note_path, t.id, "fresh notes")
        return lecture_mod.StudyOutcome(notes=StudyNotes(overview="Fresh."),
                                        study_path=study_path)

    monkeypatch.setattr(lecture_mod, "produce", produce)
    res = sync.process_transcript(cfg, transcript, llm=None)

    new_note = Path(res["note_path"])
    fresh = writer.study_note_path_for(cfg, new_note)
    assert fresh != old_study
    assert "fresh notes" in fresh.read_text(), "the move overwrote what the pass wrote"
    assert f"[[{fresh.stem}|Full study notes]]" in new_note.read_text()
