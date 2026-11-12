from datetime import datetime, timezone

import pytest

import filters
from nlp_parser import NLPQueryParser

parser = NLPQueryParser()


def parse(q):
    intent, keywords, fs = parser.parse(q)
    return intent, keywords, sorted(fs)


@pytest.mark.parametrize("query, intent", [  # S3: embedded text is the query as typed, minus filter phrases
    ("How do containers isolate processes?", "How do containers isolate processes?"),
    ("pdf reports about budgets larger than 5MB", "reports about budgets"),
])
def test_embedded_text_is_the_natural_query(query, intent):
    assert parse(query)[0] == intent


def test_keywords_stay_lemmatised_lowercase_terms_for_bm25():
    assert "budgets" in parse("pdf reports about Budgets")[1]


@pytest.mark.parametrize("query, kept", [  # S4
    ("errors in python", "python"),
    ("emails from john", "john"),
])
def test_in_and_from_only_fire_for_a_month_name(query, kept):
    intent, _, fs = parse(query)
    assert kept in intent and fs == []


def test_a_real_month_still_filters():
    assert parse("reports in march")[2] == ["month:march"]


@pytest.mark.parametrize("query", ["documents", "find docs about tax"])  # S6
def test_documents_is_not_a_type_filter(query):
    assert parse(query)[2] == []


@pytest.mark.parametrize("query, expected", [  # S8 and the "1.5GB" review finding
    ("release notes v2.0", []),
    ("node.js tutorial", []),
    ("smaller than 1.5GB", ["size:size_lt:1.5GB"]),
    ("budget.pdf", ["extension:pdf"]),
])
def test_only_known_extensions_become_filters(query, expected):
    assert parse(query)[2] == expected


@pytest.mark.parametrize("name, start, end", [  # S9: calendar days in the configured zone
    ("today", "2026-11-13T00:00:00+05:30", "2026-11-14T00:00:00+05:30"),
    ("yesterday", "2026-11-12T00:00:00+05:30", "2026-11-13T00:00:00+05:30"),
])
def test_today_and_yesterday_are_calendar_days(name, start, end):
    now = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)  # already 01:30 on the 13th in Kolkata
    f = filters.resolve([f"date:{name}"], now=now, tz="Asia/Kolkata")
    assert (f.uploaded_from, f.uploaded_to) == (start, end)
