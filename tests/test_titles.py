"""Display title formatting helpers."""
from datetime import date

from transcript_analyzer.titles import (
    clean_headline,
    compose_display_title,
    format_long_date,
    headline_from_summary,
)


def test_format_long_date_ordinals():
    assert format_long_date(date(2026, 7, 26)) == "July 26th, 2026"
    assert format_long_date(date(2026, 7, 1)) == "July 1st, 2026"
    assert format_long_date(date(2026, 7, 2)) == "July 2nd, 2026"
    assert format_long_date(date(2026, 7, 3)) == "July 3rd, 2026"
    assert format_long_date(date(2026, 7, 11)) == "July 11th, 2026"
    assert format_long_date(date(2026, 7, 12)) == "July 12th, 2026"
    assert format_long_date(date(2026, 7, 13)) == "July 13th, 2026"
    assert format_long_date(date(2026, 7, 21)) == "July 21st, 2026"


def test_compose_display_title():
    assert (
        compose_display_title("Pricing deck review with Angela", date(2026, 7, 26))
        == "Pricing deck review with Angela, July 26th, 2026"
    )
    # Strips leading ISO date / trailing long date if present.
    assert (
        compose_display_title(
            "2026-07-26 pricing-deck-review, July 26th, 2026", "2026-07-26"
        )
        == "pricing-deck-review, July 26th, 2026"
    )


def test_headline_from_summary():
    h = headline_from_summary(
        "Angela agreed to review the pricing deck by Friday. We also talked fundraising."
    )
    assert h == "Angela agreed to review the pricing deck by Friday"
    assert clean_headline("  Hello  ") == "Hello"
    assert (
        headline_from_summary("The conversation was about networking tips for founders.")
        == "Networking tips for founders"
    )


def test_compose_display_title_rejects_a_junk_date():
    import pytest

    with pytest.raises(ValueError):
        compose_display_title("Headline", "None")


def test_headline_from_summary_edge_cases():
    # No summary and no fallback still yields a usable title.
    assert headline_from_summary("") == "Untitled conversation"
    assert headline_from_summary("", fallback="2026-07-01 some-note") == "some-note"
    # A long single sentence is truncated on a word boundary with an ellipsis.
    long = headline_from_summary("word " * 60)
    assert len(long) <= 91 and long.endswith("…")
    # Re-composing an already-composed title does not stack date suffixes.
    once = compose_display_title("Pricing deck review", date(2026, 7, 26))
    assert compose_display_title(once, date(2026, 7, 26)) == once
