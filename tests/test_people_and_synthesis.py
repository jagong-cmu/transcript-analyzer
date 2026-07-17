"""R1/R2: dossiers are email-keyed and thresholded; commitments and the
synthesis steps write the right files without hallucination surface."""
from conftest import make_record

from transcript_analyzer.models import Attendee
from transcript_analyzer.pipeline import synthesize
from transcript_analyzer.pipeline.synthesize import (
    _self_keys,
    build_people,
    commitments,
    dossiers,
)


def angela(email="angela@example.com", name="Angela Jin"):
    return Attendee(name=name, email=email)


def records_with_alias_drift():
    """Same person, two display names — one note has the attendee record
    (email), the other only has the LLM-extracted alias."""
    r1 = make_record(tid="t1", title="2026-07-01 a", people=["Angela Jin"],
                     attendees=[angela()])
    r2 = make_record(tid="t2", title="2026-07-02 b", people=["Angela_jin"],
                     attendees=[angela(name="Angela Jin")])
    r3 = make_record(tid="t3", title="2026-07-03 c", people=["Angela Jin"],
                     attendees=[angela()])
    return [r1, r2, r3]


def test_email_is_identity_key():
    people = build_people(records_with_alias_drift())
    p = people["angela@example.com"]
    assert len(p.records) == 3
    assert p.display_name == "Angela Jin"
    # The underscore alias did not create a second person.
    assert "angela jin" not in people or people["angela jin"] is p


def test_self_excluded_by_config(cfg):
    people = build_people(records_with_alias_drift())
    cfg2 = cfg.__class__(**{**cfg.__dict__, "synthesis": cfg.synthesis.__class__(
        self_emails=("angela@example.com",))})
    keys = _self_keys(cfg2, people, 3)
    assert "angela@example.com" in keys


def test_self_autodetected_when_dominant(cfg):
    # Someone in >=60% of many records with no self config = the vault owner.
    recs = [
        make_record(tid=f"t{i}", title=f"2026-07-{i:02d} x",
                    people=["Jonathan Gong"],
                    attendees=[Attendee(name="Jonathan Gong", email="me@x.com")])
        for i in range(1, 11)
    ]
    people = build_people(recs)
    assert "me@x.com" in _self_keys(cfg, people, len(recs))


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, system, user, schema, **kw):
        self.calls += 1
        return self.payload


def test_dossier_threshold_and_change_detection(cfg):
    recs = records_with_alias_drift()  # Angela x3 (meets threshold)
    recs.append(make_record(tid="t9", title="2026-07-09 once", people=["One Timer"],
                            attendees=[Attendee(name="One Timer", email="ot@x.com")]))
    llm = FakeLLM({
        "who": "Angela is a design partner.",
        "cares_about": [{"text": "Cares about pricing.", "source_id": "t1",
                         "quote": "review the pricing deck"}],
        "open_threads": [],
    })
    out = dossiers(cfg, llm, recs)
    assert out["qualified"] == 1          # One Timer filtered by the >=3 threshold
    assert out["written"] == 1
    assert (cfg.vault.insights_path / "People" / "Angela Jin.md").exists()

    # Unchanged inputs -> no second API call (cost guard by change detection).
    out2 = dossiers(cfg, llm, recs)
    assert out2["written"] == 0 and out2["unchanged"] == 1
    assert llm.calls == 1


def test_commitments_mechanical(cfg):
    recs = [
        make_record(tid="t1", title="2026-07-01 a", open_items=["Send the recap"]),
        make_record(tid="t2", title="2026-07-02 b", open_items=[]),
    ]
    out = commitments(cfg, recs)
    assert out["open_items"] == 1
    text = (cfg.vault.insights_path / "Digests" / "Commitments.md").read_text()
    assert "- [ ] Send the recap" in text
    assert "[[2026-07-01 a]]" in text


def test_digest_citation_gate_drops_fabrications(cfg):
    recs = [make_record(tid="t1", title="2026-07-15 a", date_str="2026-07-15")]
    llm = FakeLLM({
        "headline": "One real thing happened.",
        "sections": [{
            "title": "Decisions",
            "claims": [
                {"text": "Angela will review the deck.", "source_id": "t1",
                 "quote": "review the pricing deck by Friday"},
                {"text": "Fabricated claim.", "source_id": "t1",
                 "quote": "this span appears nowhere"},
            ],
        }],
    })
    from datetime import date

    out = synthesize.digest(cfg, llm, recs, date(2026, 7, 16))
    assert out["dropped_claims"] == 1
    text = (cfg.vault.insights_path / "Digests" / "2026-07-16.md").read_text()
    assert "Angela will review the deck." in text
    assert "Fabricated claim." not in text
    assert "1 claim(s) dropped by the citation gate" in text
