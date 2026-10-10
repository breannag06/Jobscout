import dataclasses

import pytest
from helpers import make_job

from jobscout.filters import JobFilter

# ---- Location rules --------------------------------------------------------------


def test_intern_away_from_home_needs_subsidy(job_filter):
    plain = make_job("Data Analyst Intern", "Denver, CO")
    housed = make_job("Data Analyst Intern", "Denver, CO", "python sql. Housing stipend provided.")
    assert job_filter.score(plain) is None
    assert job_filter.score(housed) is not None


def test_away_internship_without_subsidy_is_a_scored_near_miss(job_filter):
    verdict = job_filter.evaluate(make_job("Sales Intern", "Denver, CO", "prospecting"))
    assert not verdict.passed and verdict.near_miss
    assert verdict.score > 0 and verdict.track == "sales"


def test_other_rejections_are_not_near_misses(job_filter):
    assert not job_filter.evaluate(make_job("Data Analyst", "Denver, CO", "entry level")).near_miss
    assert not job_filter.evaluate(make_job("Sales Intern", "London, UK")).near_miss


def test_negated_subsidy_does_not_count(job_filter):
    job = make_job("Data Analyst Intern", "Denver, CO", "python. No relocation assistance.")
    verdict = job_filter.evaluate(job)
    assert not verdict.passed
    assert "housing, relocation, or stipend" in verdict.rejection


def test_subsidy_requirement_can_be_turned_off(config):
    lenient = JobFilter(dataclasses.replace(config, require_subsidy_for_away_internships=False))
    assert lenient.score(make_job("Business Analyst Intern", "Washington, DC")) is not None
    assert lenient.score(make_job("Business Analyst", "Washington, DC", "entry level")) is None
    assert lenient.score(make_job("Business Analyst Intern", "London, UK")) is None


def test_albuquerque_intern_needs_no_subsidy(job_filter):
    assert job_filter.score(make_job("Data Analyst Intern", "Albuquerque, NM")) is not None


def test_albuquerque_full_time_passes(job_filter):
    job = make_job("Data Analyst", "Albuquerque, NM", "entry level, full-time, python")
    assert job_filter.score(job) is not None


@pytest.mark.parametrize(
    "title, location, description",
    [
        ("Business Development Associate", "Austin, TX", "entry level housing"),
        ("Data Analyst", "Santa Fe, New Mexico", "entry level python"),
        ("Data Analyst", "Los Alamos, NM", "entry level python part-time"),
    ],
)
def test_non_intern_outside_albuquerque_rejected(job_filter, title, location, description):
    assert job_filter.score(make_job(title, location, description)) is None


def test_remote_full_time_rejected(job_filter):
    job = make_job("Business Development Associate", "Remote - US", "entry level, full-time")
    assert job_filter.evaluate(job).rejection == "remote or unlisted, but full-time"


def test_remote_part_time_passes(job_filter):
    job = make_job("Business Development Associate", "Remote - US", "entry level, part-time role")
    verdict = job_filter.evaluate(job)
    assert verdict.passed and verdict.part_time


def test_part_time_employment_type_counts(job_filter):
    job = make_job("Campus Ambassador", "Remote", "Host events.", employment_type="part-time")
    assert job_filter.score(job) is not None


def test_part_time_in_title_counts(job_filter):
    assert job_filter.score(make_job("BDR Sales Intern (Part-Time)", "Remote", "sales")) is not None
    job = make_job("Customer Success Associate (Part Time)", "Remote - US", "entry level")
    assert job_filter.score(job) is not None


def test_flexible_schedule_in_benefits_is_not_part_time(job_filter):
    role = "entry level business development role. " + "Prospect new customers. " * 20
    job = make_job(
        "Business Development Associate", "Remote - US", role + "Benefits: flexible schedule"
    )
    assert job_filter.score(job) is None


def test_not_part_time_is_not_part_time(job_filter):
    job = make_job(
        "Business Development Associate", "Remote - US", "entry level. Not a part-time role."
    )
    assert job_filter.score(job) is None


def test_unknown_location(job_filter):
    assert job_filter.score(make_job("Data Analyst", "", "entry level python")) is None
    assert job_filter.score(make_job("Data Analyst Intern", "")) is not None


@pytest.mark.parametrize(
    "location",
    [
        "Remote, Canada",
        "Remote - Ireland",
        "Remote, EMEA; Remote, France; Remote, Germany; Remote, Ireland",
        "MX-Mexico-Remote",
    ],
)
def test_foreign_remote_rejected(job_filter, location):
    job = make_job("Data Analyst Intern", location, "python sql part-time housing")
    assert job_filter.evaluate(job).rejection == "outside the US"


@pytest.mark.parametrize(
    "location",
    [
        "Remote, North America",
        "Remote, Canada; Remote, United States",
        "Remote - US",
        "US-IL-Remote",
    ],
)
def test_us_marker_overrides_foreign_term(job_filter, location):
    assert job_filter.score(make_job("Data Analyst Intern", location)) is not None


def test_structured_country_marks_foreign_postings(job_filter):
    abroad = make_job("Supply Chain Analytics Intern", "Bratislava 22, Bratislavsky kraj")
    abroad = dataclasses.replace(abroad, country="SK")
    assert job_filter.evaluate(abroad).rejection == "outside the US"
    home = dataclasses.replace(make_job("Data Analyst Intern", "Remote"), country="US")
    assert job_filter.score(home) is not None
    both = dataclasses.replace(make_job("Data Analyst Intern", "Dublin; Remote, US"), country="IE")
    assert job_filter.score(both) is not None


@pytest.mark.parametrize(
    "title",
    ["Software Consulting Engineer I (Intern)", "AI Operations Engineer I (Intern)"],
)
def test_engineering_titles_rejected_but_sales_engineers_kept(job_filter, title):
    assert job_filter.score(make_job(title, "Remote - US")) is None
    assert job_filter.score(make_job("Solutions Engineer Intern", "Remote - US")) is not None


def test_foreign_place_in_title_rejected(job_filter):
    job = make_job("Sales Development Representative | France | Remote", "Remote", "part-time")
    assert job_filter.score(job) is None


def test_tukwila_not_treated_as_foreign(job_filter):
    job = make_job("Data Analyst Intern", "Tukwila, WA", "python sql. Relocation provided.")
    assert job_filter.score(job) is not None


def test_austin_does_not_count_as_us_marker(job_filter):
    job = make_job("Data Analyst Intern", "Austin; London", "python sql housing")
    assert job_filter.score(job) is None


# ---- Title and eligibility rules ---------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Data Analyst Intern",
        "PhD Intern, Data Science (2027)",
        "MBA Sales Intern",
        "Data Analyst Intern (Summer 2026)",
        "Summer 2027 Sales Intern (May 2028 Graduates)",
    ],
)
def test_ineligible_titles_rejected(job_filter, title):
    assert job_filter.score(make_job(title, "Remote - US")) is None


def test_software_engineer_intern_not_a_target_role(job_filter):
    assert job_filter.score(make_job("Software Engineer Intern", "Remote - US")) is None
    verdict = job_filter.evaluate(make_job("Product Design Intern", "Remote - US"))
    assert verdict.rejection == "not a target role"


def test_international_is_not_an_internship(job_filter):
    assert not job_filter.is_internship("International Sales Development Representative")
    assert job_filter.is_internship("Sales Co-op (Spring 2027)")
    assert job_filter.is_internship("Summer Analyst", "internship")


def test_international_in_description_is_not_a_level_signal(job_filter):
    job = make_job("Sales Operations Analyst", "Albuquerque, NM", "Join our international team.")
    assert job_filter.evaluate(job).rejection == "no internship, student, or entry-level signal"


def test_experience_requirement_rejected(job_filter):
    job = make_job("Data Analyst, New Grad", "Albuquerque, NM", "Requires 4+ years of experience")
    assert job_filter.evaluate(job).rejection == "asks for 4 years of experience"


def test_risk_analyst_and_partnership_internships_are_target_roles(job_filter):
    for title in ["Brokerage Risk Analyst Intern", "Crypto Partnership Intern"]:
        assert job_filter.score(make_job(title, "Remote - US")) is not None


def test_title_may_match_is_a_cheap_prefilter(job_filter):
    assert job_filter.title_may_match("Summer 2027 Sales Internship")
    assert not job_filter.title_may_match("Senior Sales Manager")
    assert not job_filter.title_may_match("Software Engineer Intern")


def test_title_rejection_explains(job_filter):
    assert job_filter.title_rejection("Senior Sales Manager") == "excluded title keyword (senior)"
    assert job_filter.title_rejection("Product Design Intern") == "not a target role"
    assert job_filter.title_rejection("SDR Intern") == ""


def test_listing_may_match_skips_clearly_foreign_listings(job_filter):
    assert job_filter.listing_may_match("Sales Intern", "Georgia - Atlanta")
    assert job_filter.listing_may_match("Sales Intern", "2 Locations")
    assert not job_filter.listing_may_match("Sales Intern", "Ireland - Dublin")
    assert not job_filter.listing_may_match("Sales Intern [Brazil]", "")


# ---- Scoring, tracks, and explanations ---------------------------------------------


def test_sales_intern_ranks_above_data_intern(job_filter):
    sales = job_filter.score(make_job("Sales Development Intern", "Remote", "python sql data"))
    data = job_filter.score(make_job("Data Analyst Intern", "Remote", "python sql data"))
    assert sales > data


def test_intern_scores_higher_than_equivalent_role(job_filter):
    intern = job_filter.score(make_job("Data Analyst Intern", "Albuquerque, NM"))
    entry = job_filter.score(
        make_job("Data Analyst", "Albuquerque, NM", "entry level python sql data")
    )
    assert intern > entry


def test_sophomore_friendly_posting_ranks_above_rising_senior_only(job_filter):
    open_to_you = job_filter.score(make_job("Data Analyst Intern", "Remote", "Sophomore welcome."))
    seniors_only = job_filter.score(
        make_job("Data Analyst Intern", "Remote", "For rising senior students in their final year.")
    )
    assert open_to_you > seniors_only


def test_evaluation_explains_score_and_track(job_filter):
    verdict = job_filter.evaluate(make_job("SDR Intern", "Remote", "prospecting with salesforce"))
    assert verdict.passed and verdict.internship
    assert verdict.track == "sales"
    assert verdict.reasons[0].startswith("title: ")
    assert any(reason.startswith("internship +") for reason in verdict.reasons)


def test_track_falls_back_to_other(job_filter):
    assert job_filter.track("Data Science Intern") == "data"
    assert job_filter.track("Growth Marketing Intern") == "other"


def test_titles_that_only_name_past_years_are_stale(config):
    from datetime import date

    no_year_keywords = dataclasses.replace(
        config,
        exclude_title_keywords=tuple(
            k for k in config.exclude_title_keywords if not k[:4].isdigit()
        ),
    )
    next_year = JobFilter(no_year_keywords, today=date(2027, 9, 1))
    assert next_year.title_rejection("Sales Intern (Summer 2026)") == "for a past year (2026)"
    assert next_year.title_rejection("Data Analyst Intern, Fall 2026 - Spring 2027") == ""
    assert next_year.title_rejection("SDR Intern Summer 2028") == ""
    assert next_year.title_rejection("SDR Intern") == ""
    verdict = next_year.evaluate(make_job("Data Analyst Intern (Summer 2026)", "Remote"))
    assert verdict.rejection == "for a past year (2026)"


# ---- Beginner sales jobs -------------------------------------------------------------


def test_new_mexico_is_not_mexico(job_filter):
    location = "2201 Louisiana Blvd NE, Albuquerque, New Mexico"
    assert job_filter.listing_may_match("Retail Sales Associate", location)
    assert job_filter.evaluate(make_job("Retail Sales Associate", location, "Sell phones.")).passed
    assert job_filter.evaluate(make_job("Sales Intern", "Mexico City, Mexico")).rejection == (
        "outside the US"
    )


def test_beginner_sales_jobs_in_albuquerque_pass_and_part_time_ranks_higher(job_filter):
    full = make_job("Full Time - Sales Associate - Paint", "Albuquerque, NM", "Help customers.")
    part = make_job(
        "Retail Sales - Part Time",
        "Albuquerque, NM",
        "Help customers.",
        employment_type="part-time",
    )
    full_verdict, part_verdict = job_filter.evaluate(full), job_filter.evaluate(part)
    assert full_verdict.passed and part_verdict.passed
    assert part_verdict.score > full_verdict.score
    assert "part-time +4" in part_verdict.reasons
    assert full_verdict.track == "sales"


@pytest.mark.parametrize(
    "title", ["Teammate - Operations", "Retail Bike Technician", "Operations ASM"]
)
def test_non_sales_retail_jobs_rejected(job_filter, title):
    job = make_job(title, "Albuquerque, NM", "Stock shelves.", employment_type="part-time")
    assert job_filter.score(job) is None


def test_operations_still_counts_for_internships(job_filter):
    assert job_filter.score(make_job("Brokerage Operations Intern", "Remote - US")) is not None


def test_beginner_sales_jobs_away_from_albuquerque_rejected(job_filter):
    job = make_job(
        "Retail Sales Associate", "Phoenix, AZ", "Sell phones.", employment_type="part-time"
    )
    assert job_filter.evaluate(job).rejection == "outside Albuquerque and not an internship"


def test_board_employment_type_beats_description_text(job_filter):
    job = make_job(
        "Full Time - Sales Associate",
        "Albuquerque, NM",
        "Full-time and part-time positions available.",
        employment_type="full-time",
    )
    verdict = job_filter.evaluate(job)
    assert verdict.passed and not verdict.part_time


def test_structured_us_country_is_never_foreign(job_filter):
    job = dataclasses.replace(make_job("Sales Intern", "Paris, Texas"), country="US")
    assert job_filter.evaluate(job).rejection != "outside the US"
    assert job_filter.evaluate(job).near_miss  # an away internship, not a foreign one


def test_supervisors_are_not_beginner_jobs(job_filter):
    job = make_job(
        "Sales Supervisor, Part Time",
        "Albuquerque, NM",
        "Lead the team.",
        employment_type="part-time",
    )
    assert job_filter.score(job) is None


def test_commission_only_listings_sink_to_the_bottom(job_filter):
    hourly = make_job("Sales Associate", "Albuquerque, NM", "Paid training. $16 per hour.")
    commission = make_job(
        "Sales Associate", "Albuquerque, NM", "Paid training. 100% commission, be your own boss."
    )
    assert job_filter.score(commission) < job_filter.score(hourly) - 30
