"""Course identity for lecture recordings, without a course registry.

The captain declined a registry in config, so a course exists only because a
model named it on a lecture. That makes BINDING the whole problem: week 2 of
21-241 must land on the same course as week 1, which the model may have
written "21241", "21 241" or "Linear Algebra (21241)".

The rule here is deliberately narrow. `canonical_code` reduces a code to the
digits and letters it is made of, which folds every spelling of one code
together; `bind` then prefers a code ALREADY IN THE INDEX over a new one, so
the first spelling a course got is the one it keeps. Nothing fuzzy-matches
names — two different courses can share most of a title ("Intro to X" /
"Intro to Y"), and merging them would be worse than the accepted risk of a
course occasionally splitting in two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# A course code as the model may write it: "21-241", "21241", "15 150",
# "CS 15150". Letters are kept (some schools use them) but case is folded.
_CODE_CHARS = re.compile(r"[^0-9a-z]+")
# Codes short enough to collide with anything ("A", "12") are not identity.
MIN_CODE_LEN = 4


@dataclass(frozen=True)
class Course:
    """A course as the index knows it: canonical key, and how it is written."""

    key: str  # canonical_code(code) — the identity
    code: str  # display form, as first recorded
    name: str = ""


def canonical_code(code: str) -> str:
    """The identity form of a course code: lowercase, punctuation removed.

    "21-241", "21 241" and "21241" are one course; "" stays "" (a lecture may
    genuinely have no code, and an empty key must never match another empty).
    """
    return _CODE_CHARS.sub("", str(code or "").lower())


def is_usable_code(code: str) -> bool:
    """Whether a code is specific enough to key a course on."""
    return len(canonical_code(code)) >= MIN_CODE_LEN


def index_courses(rows: Iterable[tuple[str, str]]) -> dict[str, Course]:
    """Build the known-course table from (course_code, course_name) pairs.

    Rows arrive newest-first from the index; the FIRST usable spelling of a
    key wins, so the display form stays stable as long as that note exists
    rather than flipping each time the model writes the code differently.
    """
    known: dict[str, Course] = {}
    for code, name in rows:
        key = canonical_code(code)
        if not key or len(key) < MIN_CODE_LEN or key in known:
            continue
        known[key] = Course(key=key, code=str(code).strip(), name=str(name or "").strip())
    return known


def bind(
    code: str, name: str, known: dict[str, Course]
) -> tuple[str, str]:
    """Normalize a model-emitted (code, name) against the courses already seen.

    Returns the (code, name) to store. A code that matches a known course
    takes that course's spelling and — when the model gave no name — its name,
    which is what makes every week of one course collapse into one entity. An
    unusable code (too short, or absent) keeps only the name, so a lecture with
    no code is still labelled but never collides with another under an empty key.
    """
    code = str(code or "").strip()
    name = str(name or "").strip()
    if not is_usable_code(code):
        return "", name
    prior = known.get(canonical_code(code))
    if prior is None:
        return code, name
    return prior.code, (name or prior.name)


def find(code: str, known: dict[str, Course]) -> Optional[Course]:
    """The known course a code refers to, or None."""
    return known.get(canonical_code(code)) if is_usable_code(code) else None
