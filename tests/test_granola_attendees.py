"""R2: granola.py used to do `add(name or email)` — the email (the stable
identity key) was discarded whenever a name existed. Now both survive."""
from transcript_analyzer.connectors.granola import GranolaClient


def test_email_persisted_alongside_name():
    detail = {"attendees": [{"name": "Angela Jin", "email": "angela@example.com"}]}
    atts = GranolaClient._attendees(detail)
    assert atts[0].name == "Angela Jin"
    assert atts[0].email == "angela@example.com"
    assert atts[0].key == "angela@example.com"


def test_dedupe_by_identity_key():
    detail = {"attendees": [
        {"name": "Angela Jin", "email": "Angela@Example.com"},
        {"name": "Angela", "email": "angela@example.com"},
    ]}
    assert len(GranolaClient._attendees(detail)) == 1


def test_string_attendees():
    detail = {"attendees": ["bob@example.com", "Carol"]}
    atts = GranolaClient._attendees(detail)
    assert atts[0].email == "bob@example.com" and atts[0].name == ""
    assert atts[1].name == "Carol" and atts[1].email == ""
