"""Week 2 of a course has to land on the same course as week 1.

There is no course registry in config, so binding is the whole mechanism: a
model that writes "21-241" this week and "21241" next week must not create two
courses, and two genuinely different courses must not be merged.
"""
from transcript_analyzer.courses import (
    Course,
    bind,
    canonical_code,
    find,
    index_courses,
    is_usable_code,
)


def test_spellings_of_one_code_are_one_course():
    assert canonical_code("21-241") == canonical_code("21241") == canonical_code("21 241")
    assert canonical_code("CS 15150") == "cs15150"


def test_a_code_too_short_to_identify_anything_is_not_used():
    assert not is_usable_code("21")
    assert not is_usable_code("")
    assert is_usable_code("21241")


def test_the_first_spelling_a_course_gets_is_the_one_it_keeps():
    known = index_courses(
        [("21-241", "Linear Algebra"), ("21241", ""), ("15150", "Functional Programming")]
    )
    assert set(known) == {"21241", "15150"}
    assert known["21241"].code == "21-241"
    assert known["21241"].name == "Linear Algebra"


def test_a_new_lecture_binds_to_the_course_already_in_the_vault():
    known = index_courses([("21-241", "Linear Algebra")])
    # Next week the model writes it differently and forgets the name.
    assert bind("21241", "", known) == ("21-241", "Linear Algebra")


def test_a_model_supplied_name_wins_over_the_stored_one():
    known = index_courses([("21-241", "Linear Algebra")])
    assert bind("21 241", "Matrices and Linear Transformations", known) == (
        "21-241",
        "Matrices and Linear Transformations",
    )


def test_an_unknown_course_is_kept_as_written():
    assert bind("15150", "Functional Programming", {}) == (
        "15150",
        "Functional Programming",
    )


def test_a_lecture_with_no_usable_code_keeps_only_its_name():
    """Two nameless lectures must not collide under an empty key."""
    known = index_courses([("21241", "Linear Algebra")])
    assert bind("", "Guest talk on HCI", known) == ("", "Guest talk on HCI")
    assert bind("A", "Seminar", known) == ("", "Seminar")


def test_names_are_never_fuzzy_matched():
    """'Intro to X' and 'Intro to Y' are different courses; only codes bind."""
    known = index_courses([("21241", "Introduction to Linear Algebra")])
    code, name = bind("15150", "Introduction to Functional Programming", known)
    assert (code, name) == ("15150", "Introduction to Functional Programming")


def test_find_returns_the_bound_course_or_nothing():
    known = index_courses([("21-241", "Linear Algebra")])
    assert find("21241", known) == Course(key="21241", code="21-241", name="Linear Algebra")
    assert find("99999", known) is None
    assert find("", known) is None
