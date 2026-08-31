"""A re-worded headline renames the note; its recording has to follow.

Audio is keyed on the note stem, so reprocessing a transcript whose headline
changes left the old mp3 orphaned in Attachments/ — and, worse, the new stem
did not exist, so download_audio's "already downloaded" short-circuit missed
and the whole recording was fetched again.
"""
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from transcript_analyzer import sync
from transcript_analyzer.db import get_conn, record_sync
from transcript_analyzer.models import Insight, Transcript
from transcript_analyzer.obsidian import writer


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


def test_move_audio_with_note_replaces_a_stale_file_at_the_target(cfg):
    old_note = cfg.vault.insights_path / "2026-07-01 old.md"
    new_note = cfg.vault.insights_path / "2026-07-01 new.md"
    old_audio = writer.audio_path_for(cfg, old_note)
    new_audio = writer.audio_path_for(cfg, new_note)
    old_audio.parent.mkdir(parents=True, exist_ok=True)
    old_audio.write_bytes(b"keep-me")
    new_audio.write_bytes(b"stale")

    assert writer.move_audio_with_note(cfg, old_note, new_note) == new_audio
    assert new_audio.read_bytes() == b"keep-me"
    assert not old_audio.exists()

    # Nothing to move: same note, or no recording on disk.
    assert writer.move_audio_with_note(cfg, new_note, new_note) is None
    assert writer.move_audio_with_note(cfg, old_note, new_note) is None


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
