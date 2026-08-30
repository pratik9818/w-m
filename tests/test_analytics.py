"""Telling an owner who visited their website.

The feature exists because of one sentence from an owner: they do not type /traffic, they
type "how many visit today or yesterday or this week or month or year, where traffic comes
from". So most of what is tested here is comprehension -- that ordinary English about
visitors is recognised, that it is told apart from an instruction that happens to contain
the same words, and that the period asked for is the period counted.

The rest is about honesty, which matters more here than anywhere else in the bot. A
visitor count is the number an owner uses to decide whether any of this was worth doing.
Three ways of getting it wrong are each pinned by a test below: counting crawlers as
customers, reporting "nobody came" for a stretch when nothing was counting, and
double-counting a visit because the page picked up two beacons.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bot_api.services import analytics
from bot_api.services.analytics import (
    ASPECT_DEVICES,
    ASPECT_PAGES,
    ASPECT_PLACES,
    ASPECT_SOURCES,
    ASPECT_TOTAL,
    AnalyticsUnavailable,
    Window,
    aspects_for,
    default_window,
    describe_source,
    looks_like_a_traffic_question,
    parse_window,
    render_full_report,
    render_traffic_answer,
    traffic_reply,
)

# A Wednesday, so "this week" has days on both sides of it and cannot pass by accident.
NOW = datetime(2026, 8, 26, 15, 30, tzinfo=timezone.utc)


class FakeBusiness:
    def __init__(self, site_tag="tag123", enabled_at=None, name="Rise & Crumb"):
        self.name = name
        self.cf_rum_site_tag = site_tag
        self.cf_rum_site_token = "token" if site_tag else None
        self.analytics_enabled_at = enabled_at


def _data(window, visits=0, previous=0, **overrides):
    data = {
        "window": window,
        "visits": visits,
        "visits_previous": previous,
        "pageviews": visits,
        "per_day": [],
        "sources": [],
        "countries": [],
        "pages": [],
        "devices": [],
    }
    data.update(overrides)
    return data


# --------------------------------------------------- recognising the question

@pytest.mark.parametrize("message", [
    "how many visits today",
    "How many people visited yesterday?",
    "did anyone look at my site this week",
    "any visitors this month?",
    "hows my traffic",
    "where does my traffic come from",
    "where are these people coming from?",
    "which page is most popular",
    "how many views this year",
    "show me my stats",
])
def test_ordinary_questions_about_visitors_are_recognised(message):
    """None of these say "analytics" and none of them are a command.

    This is the whole feature: an owner who has never seen a dashboard asks in the words
    they already have, and gets an answer rather than being told to phrase it differently.
    """
    assert looks_like_a_traffic_question(message)


@pytest.mark.parametrize("message", [
    "add a visitor counter to my home page",
    "make the views section bigger",
    "change my phone number",
    "put a photo at the top",
    "remove the traffic light picture",
    "which page has my phone number on it",
])
def test_edits_that_mention_visitors_are_not_mistaken_for_questions(message):
    """The dangerous direction of the two.

    Answering "add a visitor counter" with a visitor count is merely unhelpful. But this
    check runs before the edit pipeline, so a false positive here means a real change to a
    real site silently never happens -- the owner asks, is handed a number, and their site
    is untouched.
    """
    assert not looks_like_a_traffic_question(message)


# The messages below are verbatim from a real conversation, in the order they were sent.
# The owner asked about traffic, was answered, then asked for an edit and was answered with
# traffic again -- twice more, including once after saying "I don't want this". Two things
# let it through: "desktop view" contains "view", and the instruction guard only recognised
# a message that *opened* with one of about fifteen verbs, of which "center" was not one.

@pytest.mark.parametrize("message", [
    "Center form in desktop view and please make text color black beauese where i am "
    "writting it is not visible",
    "I don't want this I want Center form in desktop view and please make text color black",
    "make the mobile view better",
    "fix the tablet view",
    "also center the logo",
    "now change the phone number",
    "i want to add a booking form",
    "please align the buttons",
])
def test_an_edit_is_never_answered_with_a_visitor_chart(message):
    """The bug this guards against had no error and no way back.

    The owner's words were discarded, the reply looked like a normal answer, and repeating
    the request produced the same chart again. Nothing in the logs said anything was wrong.
    """
    assert not looks_like_a_traffic_question(message)


@pytest.mark.parametrize("message", [
    "how many visitors on mobile view",
    "how many people viewed the page I added?",
])
def test_a_real_question_survives_the_guard(message):
    """The fix cuts "mobile view" out and lets an imperative anywhere veto the question, so
    both directions need holding: these still have to reach the visitor numbers."""
    assert looks_like_a_traffic_question(message)


def test_where_from_asks_about_referrers_and_countries_together():
    """"Where do my visitors come from" is two questions in one and the words do not say
    which. Both are answered, because guessing wrong means answering neither."""
    aspects = aspects_for("where are my visitors coming from?")
    assert ASPECT_SOURCES in aspects
    assert ASPECT_PLACES in aspects


def test_a_bare_count_question_asks_for_nothing_else():
    assert aspects_for("how many visits today") == {ASPECT_TOTAL}


def test_specific_questions_get_the_specific_part():
    assert ASPECT_PAGES in aspects_for("what's my most popular page?")
    assert ASPECT_DEVICES in aspects_for("are people looking on their phones?")


# --------------------------------------------------- the period they asked about

def test_today_is_one_day_and_compares_against_yesterday():
    window = parse_window("how many visits today", now=NOW)
    assert window.label == "today"
    assert window.is_single_day
    assert window.start == datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert window.previous_label == "yesterday"


def test_yesterday_excludes_today():
    window = parse_window("did anyone visit yesterday", now=NOW)
    assert window.start == datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert window.end == datetime(2026, 8, 26, tzinfo=timezone.utc)


def test_this_week_starts_on_monday():
    """Not "the last seven days". An owner asking how this week is going means the week
    they are in, and answering with a rolling window quietly includes last Thursday."""
    window = parse_window("how's this week looking", now=NOW)
    assert window.start == datetime(2026, 8, 24, tzinfo=timezone.utc)  # the Monday


def test_last_week_is_the_completed_week_not_the_last_seven_days():
    """These are different questions and owners mean both literally. "Last week" that
    silently included today would report a week nobody asked about."""
    window = parse_window("how many visits last week", now=NOW)
    assert window.start == datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert window.end == datetime(2026, 8, 24, tzinfo=timezone.utc)


def test_this_month_starts_at_the_first():
    window = parse_window("visits this month", now=NOW)
    assert window.start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_last_month_is_the_whole_previous_calendar_month():
    window = parse_window("how did last month go", now=NOW)
    assert window.start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert window.end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_last_n_days_is_taken_literally():
    window = parse_window("visits in the last 14 days", now=NOW)
    assert window.days == 14


def test_a_year_is_capped_at_what_cloudflare_still_holds():
    """Cloudflare keeps a rolling window, not a calendar. Reporting "this year" from a
    January that no longer exists in the data would present a partial count as a whole
    one -- the number would be wrong and would look authoritative."""
    window = parse_window("how many visits this year", now=NOW)
    assert window.days <= analytics.MAX_LOOKBACK_DAYS + 1


def test_a_question_with_no_period_gets_one_that_suits_it():
    """A count means "lately" -- a week. A question about where people come from needs
    enough traffic to have a shape, so it gets a month."""
    assert parse_window("how many visits", now=NOW) is None
    assert default_window({ASPECT_TOTAL}, now=NOW).days == 7
    assert default_window({ASPECT_SOURCES, ASPECT_PLACES}, now=NOW).days == 30


# --------------------------------------------------- what is actually asked of Cloudflare

def test_crawlers_are_never_counted_as_customers():
    """Most early traffic to a new small-business site is bots. Counting them turns
    "nobody has found you yet" into "twelve people came", which is the single most
    misleading thing this feature could say."""
    built = analytics._filter("tag123", NOW - timedelta(days=1), NOW)
    assert {"bot": 0} in built["AND"]
    assert {"siteTag": "tag123"} in built["AND"]


def test_the_comparison_period_is_fetched_in_the_same_round_trip(monkeypatch):
    """"Up from last week" is the half of the answer an owner actually acts on, and a
    second query for it would double the latency of every question."""
    captured = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {"viewer": {"accounts": [{}]}}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(json["variables"])
            return FakeResponse()

    monkeypatch.setattr(analytics.httpx, "AsyncClient", lambda **kw: FakeClient())
    monkeypatch.setattr(analytics, "get_settings",
                        lambda: type("S", (), {"cloudflare_api_token": "t",
                                               "cloudflare_account_id": "a"})())

    window = Window("today", datetime(2026, 8, 26, tzinfo=timezone.utc),
                    datetime(2026, 8, 27, tzinfo=timezone.utc), "yesterday")
    import asyncio
    asyncio.run(analytics.fetch_traffic("tag123", window))

    trend_start = captured["trend"]["AND"][0]["datetime_geq"]
    assert trend_start.startswith("2026-08-25"), "the previous day was not included"


def test_the_daily_series_is_split_at_the_window_start():
    """The trend query spans two periods; nothing else in the pipeline knows where the
    seam is, so a mistake here silently reports last week's visitors as this week's."""
    window = Window("today", datetime(2026, 8, 26, tzinfo=timezone.utc),
                    datetime(2026, 8, 27, tzinfo=timezone.utc), "yesterday")
    shaped = analytics._shape({
        "totals": [{"count": 30, "sum": {"visits": 12}}],
        "byDate": [
            {"dimensions": {"date": "2026-08-25"}, "sum": {"visits": 4}},
            {"dimensions": {"date": "2026-08-26"}, "sum": {"visits": 9}},
        ],
    }, window)
    assert shaped["visits"] == 9
    assert shaped["visits_previous"] == 4


# --------------------------------------------------- saying it

def test_one_visitor_is_a_person_not_one_visits():
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=1), {ASPECT_TOTAL}, "Rise & Crumb")
    assert "1 person" in reply
    assert "1 visits" not in reply


def test_a_small_drop_is_not_reported_as_a_percentage():
    """Three visitors becoming two is "down 33%", which is true and useless. An owner told
    that about their new site reasonably concludes it is failing."""
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=2, previous=3), {ASPECT_TOTAL}, "X")
    assert "%" not in reply


def test_a_small_drop_is_still_reported_in_plain_numbers():
    """Silent is not the same as hidden. The owner asked; they get the comparison, just
    not dressed up as a trend."""
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=2, previous=3), {ASPECT_TOTAL}, "X")
    assert "Down from 3 yesterday" in reply


def test_a_real_change_on_real_numbers_is_reported_as_a_percentage():
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=60, previous=20), {ASPECT_TOTAL}, "X")
    assert "200%" in reply


def test_zero_visits_says_so_and_says_what_to_do_about_it():
    """No path in this bot ends without a next step, and this is the path most likely to
    make an owner give up -- so it is the one that needs the next step most."""
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=0), {ASPECT_TOTAL}, "Rise & Crumb")
    assert "Nobody" in reply
    assert "Google business listing" in reply or "customers" in reply


def test_the_same_search_engine_is_not_listed_three_times():
    """Cloudflare reports google.com, www.google.com and google.co.uk separately. Three
    Google rows tell an owner less than one Google row does."""
    window = default_window({ASPECT_SOURCES}, now=NOW)
    reply = render_traffic_answer(
        _data(window, visits=30, sources=[("google.com", 10), ("www.google.com", 8),
                                          ("google.co.uk", 4)]),
        {ASPECT_SOURCES}, "X",
    )
    assert reply.count("Google") == 1
    assert "22" in reply, "the merged rows were not added together"


def test_traffic_with_no_referrer_is_described_as_a_shared_link():
    """The empty referrer is the most important row on the list for a small business: it
    is the customers they told themselves. Shown raw it is a blank line."""
    assert describe_source("") == analytics.DIRECT_SOURCE
    assert describe_source("l.facebook.com") == "Facebook"
    assert describe_source("some-directory.co.uk") == "some-directory.co.uk"


def test_a_bare_count_offers_the_next_question():
    window = parse_window("visits today", now=NOW)
    reply = render_traffic_answer(_data(window, visits=5), {ASPECT_TOTAL}, "X")
    assert "where did they come from" in reply.lower()


@pytest.mark.parametrize("question, expected", [
    ("visits today", "visited <b>X</b> today."),
    ("visits yesterday", "visited <b>X</b> yesterday."),
    ("visits this month", "visited <b>X</b> this month."),
    ("visits last week", "visited <b>X</b> last week."),
    ("visits in the last 14 days", "visited <b>X</b> in the last 14 days."),
])
def test_the_period_reads_as_english(question, expected):
    """"in this month" and "in last week" are what a template that bolts "in" onto every
    label produces, and they read as though nobody looked at the output."""
    window = parse_window(question, now=NOW)
    reply = render_traffic_answer(_data(window, visits=5), {ASPECT_TOTAL}, "X")
    assert expected in reply


def test_the_note_about_when_counting_started_comes_last():
    """It qualifies every number above it. Printed mid-report -- above, say, a busiest-day
    line naming a day before counting began -- it reads as a contradiction."""
    window = parse_window("visits last week", now=NOW)
    report = render_full_report(
        _data(window, visits=63, per_day=[(NOW.date() - timedelta(days=3), 21)]),
        "X", since=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert report.rindex("started counting") > report.rindex("Busiest day")


def test_the_full_report_carries_the_timezone_caveat_and_the_conversation_does_not():
    """Someone reading rows of numbers is owed the caveat. Someone who asked "how many
    today" is owed a sentence, and hanging a timezone note off it helps nobody."""
    window = default_window({ASPECT_TOTAL}, now=NOW)
    data = _data(window, visits=5, per_day=[(NOW.date(), 5)])
    assert "UTC" in render_full_report(data, "X")
    assert "UTC" not in render_traffic_answer(data, {ASPECT_TOTAL}, "X")


# --------------------------------------------------- zero is not the same as nothing

def test_a_site_that_was_never_counting_says_so_rather_than_reporting_zero():
    """The failure this guards against is subtle and total: a site built before analytics
    existed has no beacon, so every honest query returns zero, and the owner is told
    nobody has ever visited their working website."""
    import asyncio
    reply = asyncio.run(traffic_reply(FakeBusiness(site_tag=None), "how many visits today"))
    assert "not counting" in reply.lower()
    assert "0" not in reply


def test_a_period_before_counting_started_is_not_reported_as_empty():
    """A site that started counting yesterday answers "how many last month?" with a
    truthful zero that means "nothing was watching" and reads as "nobody came"."""
    import asyncio
    business = FakeBusiness(enabled_at=datetime(2026, 8, 20, tzinfo=timezone.utc))
    reply = asyncio.run(traffic_reply(business, "how many visits last month"))
    assert "20 August" in reply


def test_a_partly_covered_period_says_where_the_numbers_start():
    window = Window("this month", datetime(2026, 8, 1, tzinfo=timezone.utc),
                    datetime(2026, 8, 27, tzinfo=timezone.utc), "last month")
    reply = render_traffic_answer(
        _data(window, visits=12), {ASPECT_TOTAL}, "X",
        since=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert "20 August" in reply


def test_a_message_about_anything_else_costs_no_network_call():
    """traffic_reply runs before the assistant on every non-edit message. Returning None
    without touching Cloudflare is what keeps that free."""
    import asyncio

    def explode(*args, **kwargs):
        raise AssertionError("Cloudflare was called for a message that was not about traffic")

    assert asyncio.run(traffic_reply(FakeBusiness(), "what's my link?")) is None


def test_a_cloudflare_outage_becomes_a_sentence_not_an_exception(monkeypatch):
    """This runs inside the message handler. An exception here takes down the reply to a
    question that was never about visitors in the first place."""
    import asyncio

    async def fail(*args, **kwargs):
        raise AnalyticsUnavailable("boom")

    monkeypatch.setattr(analytics, "fetch_traffic", fail)
    reply = asyncio.run(traffic_reply(FakeBusiness(), "how many visits today"))
    assert "couldn't get your visitor numbers" in reply


# --------------------------------------------------- the beacon that does the counting

def test_the_beacon_goes_into_every_page_and_only_into_pages():
    from worker.tasks.web_analytics import BEACON_SRC, inject_beacon

    files = {"index.html": "<html><body>hi</body></html>",
             "about.html": "<html><body>hi</body></html>",
             "style.css": "body{}"}
    out = inject_beacon(files, "tok")
    assert all(BEACON_SRC in out[name] for name in ("index.html", "about.html"))
    assert out["style.css"] == "body{}"
    assert "tok" in out["index.html"]


def test_a_redeploy_does_not_add_a_second_beacon():
    """Redeploys hand back the stored bytes, which already carry one. Two beacons on a
    page report every visit twice, and the owner's numbers quietly double."""
    from worker.tasks.web_analytics import BEACON_SRC, inject_beacon

    once = inject_beacon({"index.html": "<html><body>hi</body></html>"}, "tok")
    twice = inject_beacon(once, "tok")
    assert twice["index.html"].count(BEACON_SRC) == 1


def test_no_token_means_the_pages_are_left_exactly_as_they_were():
    from worker.tasks.web_analytics import inject_beacon

    files = {"index.html": "<html><body>hi</body></html>"}
    assert inject_beacon(files, None) == files


def _rum_stub(monkeypatch, listed, on_create=None):
    """A fake Cloudflare that records every create call against the RUM API."""
    import httpx

    from worker.tasks import web_analytics

    created = []

    def handler(request):
        url = str(request.url)
        if request.method == "GET" and "/rum/site_info/list" in url:
            return httpx.Response(200, json={"success": True, "result": listed})
        if request.method == "POST" and url.endswith("/rum/site_info"):
            created.append(url)
            return httpx.Response(200, json={"success": True, "result": on_create})
        return httpx.Response(404, json={"success": False, "errors": [{"message": url}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web_analytics.httpx, "AsyncClient",
                        lambda *a, **kw: real_client(*a, **kw,
                                                     transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(web_analytics, "get_settings",
                        lambda: type("S", (), {"cloudflare_api_token": "t",
                                               "cloudflare_account_id": "a"})())
    return created


def test_an_existing_site_is_reused_instead_of_creating_a_second_one(monkeypatch):
    """Cloudflare does not reject a duplicate host -- verified live, where two calls a
    second apart returned two different site tags for the same hostname.

    So there is no conflict to catch after the fact: creating blind splits the site's
    history in two, the owner's numbers restart at zero, and no error is raised anywhere.
    The lookup has to happen *before* the create, not as its fallback.
    """
    import asyncio

    from worker.tasks import web_analytics

    created = _rum_stub(monkeypatch, listed=[
        {"site_tag": "existing", "site_token": "tok", "host": "looksalon.pages.dev"},
    ])
    result = asyncio.run(web_analytics.ensure_analytics_site("looksalon.pages.dev"))

    assert result == ("existing", "tok")
    assert created == [], "a second Web Analytics site was created for a host that had one"


def test_a_site_added_through_the_dashboard_is_recognised(monkeypatch):
    """Cloudflare stores a dashboard-created site's host as a regex fragment --
    "(xtravu.pages.dev)$" was found on a real account. Matching the raw string misses it,
    and the miss is worst here: that site already has the owner's history on it."""
    import asyncio

    from worker.tasks import web_analytics

    created = _rum_stub(monkeypatch, listed=[
        {"site_tag": "dash", "site_token": "tok", "host": "(xtravu.pages.dev)$"},
    ])
    result = asyncio.run(web_analytics.ensure_analytics_site("xtravu.pages.dev"))

    assert result == ("dash", "tok")
    assert created == []


def test_a_host_with_no_site_yet_does_get_one_created(monkeypatch):
    """The other half of the check above: looking first must not stop it provisioning."""
    import asyncio

    from worker.tasks import web_analytics

    created = _rum_stub(
        monkeypatch,
        listed=[{"site_tag": "other", "site_token": "tok", "host": "somewhere.else"}],
        on_create={"site_tag": "fresh", "site_token": "newtok"},
    )
    result = asyncio.run(web_analytics.ensure_analytics_site("brand-new.pages.dev"))

    assert result == ("fresh", "newtok")
    assert len(created) == 1


def test_provisioning_failure_never_stops_a_deploy(monkeypatch):
    """A site published without a beacon has lost a number nobody has yet. A site that
    failed to publish because the analytics API was unwell has lost the owner their
    website. Everything in web_analytics swallows its own failures for this reason."""
    import asyncio

    import httpx

    from worker.tasks import web_analytics

    def handler(request):
        return httpx.Response(403, json={"success": False,
                                         "errors": [{"message": "Authentication error"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web_analytics.httpx, "AsyncClient",
                        lambda *a, **kw: real_client(*a, **kw,
                                                     transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(web_analytics, "get_settings",
                        lambda: type("S", (), {"cloudflare_api_token": "t",
                                               "cloudflare_account_id": "a"})())

    assert asyncio.run(web_analytics.ensure_analytics_site("x.pages.dev")) is None


def test_a_first_deploy_uploads_pages_that_carry_the_beacon(monkeypatch):
    """Driven through the real deploy path, reading the bytes Cloudflare was handed.

    Every unit above can be correct and simply not be wired in, which is the failure mode
    that matters here: an owner asks how many people visited, gets a confident zero, and
    nothing was ever counting because the injection ran after the upload -- or not at all.
    """
    import asyncio
    import base64
    import json as jsonlib

    import httpx

    from worker.tasks import deploy as deploy_module
    from worker.tasks import web_analytics
    from worker.tasks.web_analytics import BEACON_SRC

    uploaded: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/rum/site_info"):
            return httpx.Response(200, json={"success": True, "result": {
                "site_tag": "abc123", "site_token": "tok999"}})
        if request.method == "POST" and url.endswith("/pages/projects"):
            return httpx.Response(200, json={"success": True,
                                             "result": {"subdomain": "rise-6hf.pages.dev"}})
        if url.endswith("/upload-token"):
            return httpx.Response(200, json={"success": True, "result": {"jwt": "t"}})
        if url.endswith("/pages/assets/upload"):
            for item in jsonlib.loads(request.content):
                uploaded[item["metadata"]["contentType"]] = base64.b64decode(
                    item["value"]).decode("utf-8")
            return httpx.Response(200, json={"success": True})
        if url.endswith("/deployments"):
            return httpx.Response(200, json={"success": True, "result": {"url": "u"}})
        return httpx.Response(404, json={"success": False, "errors": [{"message": url}]})

    real_client = httpx.AsyncClient
    fake_settings = type("S", (), {"cloudflare_account_id": "a", "cloudflare_api_token": "t"})
    for module in (deploy_module, web_analytics):
        monkeypatch.setattr(module.httpx, "AsyncClient",
                            lambda *a, **kw: real_client(*a, **kw,
                                                         transport=httpx.MockTransport(handler)))
        monkeypatch.setattr(module, "get_settings", lambda: fake_settings())

    class NewBusiness:
        slug = "rise-and-crumb"
        cf_pages_project_name = None
        cf_rum_site_tag = None
        cf_rum_site_token = None
        analytics_enabled_at = None

    business = NewBusiness()
    asyncio.run(deploy_module.deploy_to_cloudflare_pages(
        business, {"index.html": "<html><body>hi</body></html>"}))

    assert BEACON_SRC in uploaded["text/html"], "the deployed page cannot count anything"
    assert "tok999" in uploaded["text/html"]
    # Without this the next deploy provisions a second Web Analytics site for the same
    # hostname, and the owner's history restarts at zero every time they edit anything.
    assert business.cf_rum_site_tag == "abc123"
    assert business.analytics_enabled_at is not None


def test_deploy_puts_the_beacon_in_before_it_hashes_the_files():
    """The upload manifest is keyed on a hash of each file. A beacon added after the
    manifest is built uploads content that does not match its own hash, and Cloudflare
    serves the un-beaconed copy -- a site that looks instrumented and counts nothing."""
    import inspect

    from worker.tasks import deploy

    source = inspect.getsource(deploy.deploy_to_cloudflare_pages)
    assert source.index("inject_beacon") < source.index("manifest = {")
