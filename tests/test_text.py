import pytest

from jobscout.text import (
    find_unnegated,
    normalize_employment_type,
    role_section,
    strip_html,
    term_pattern,
    years_required,
)


def test_strip_html_collapses_markup_and_entities():
    assert strip_html("<p>Hello&nbsp;<b>World</b></p>") == "Hello World"


@pytest.mark.parametrize(
    "terms, text, expected",
    [
        (("uk",), "tukwila, wa", False),
        (("uk",), "london, uk", True),
        (("us",), "austin, tx", False),
        (("us",), "remote - us", True),
        (("u.s.",), "remote, u.s.", True),
        (("intern",), "international sales", False),
        (("co-op",), "sales co-op", True),
        ((), "anything", False),
    ],
)
def test_term_pattern_matches_whole_words(terms, text, expected):
    assert bool(term_pattern(terms).search(text)) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("no years here", 0),
        ("3 years of experience", 3),
        ("3+ years of relevant sales experience", 3),
        ("2-4 years of experience", 2),
        ("5 or more years of experience", 5),
        ("founded 15 years ago. 1 year of experience preferred", 1),
        ("we have served customers for 20 years", 0),
    ],
)
def test_years_required_only_counts_experience(text, expected):
    assert years_required(text) == expected


@pytest.mark.parametrize(
    "text, found",
    [
        ("housing stipend provided", "housing"),
        ("no relocation assistance is offered", None),
        ("relocation is not available for this role", None),
        ("we do not offer housing", None),
        ("this is not a part-time role", None),
        ("a part-time role, 20 hours per week", "part-time"),
        ("no relocation. housing provided.", "housing"),
    ],
)
def test_find_unnegated(text, found):
    assert find_unnegated(text, ["relocation", "housing", "stipend", "part-time"]) == found


def test_role_section_drops_trailing_benefits():
    description = (
        "x" * 100 + " benefits of our product " + "y" * 200 + " Benefits: flexible schedule"
    )
    section = role_section(description)
    assert "benefits of our product" in section
    assert "flexible schedule" not in section


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Part time", "part-time"),
        ("PartTime", "part-time"),
        ("Full-Time", "full-time"),
        ("Intern", "internship"),
        ("Contractor", "contract"),
        (None, ""),
        ("Weird", ""),
    ],
)
def test_normalize_employment_type(raw, expected):
    assert normalize_employment_type(raw) == expected
