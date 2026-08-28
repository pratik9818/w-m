"""Answering "how many people came to my site?" -- the question the whole thing is for.

An owner does not care about tokens, layouts or canonical tags. They care whether anyone
turned up, and until now the bot could not tell them, because nothing was counting. The
beacon added in worker/tasks/web_analytics.py counts; this module reads the count back and
says it in a sentence.

Three things shape everything here:

  1. **Nobody types /traffic.** They type "how many visits today", "did anyone look at it
     yesterday", "where are these people coming from". So the parsing is of ordinary
     English, and the slash command is a convenience for the few who like commands, not
     the way in.
  2. **No model call.** A visit count is a number in a table, and paying a language model
     to read a table out loud would be paying for a worse version of a lookup -- the same
     reasoning that keeps "what's my link" free in assistant.py. The model is involved only
     when the question is genuinely open ("is my site doing well?"), and even then it is
     handed these numbers as facts rather than asked to recall them.
  3. **Zero is not the same as nothing.** "No visits this week" is a real, disappointing
     answer. "No visits this week" from a site that only started counting on Tuesday is a
     lie shaped like one. `analytics_enabled_at` is what keeps those apart, and every
     rendering path below checks it.

Days are counted in UTC, which is what Cloudflare records. For an owner in London that is
right or an hour out; further afield "today" can disagree with their clock by most of a
morning. The detailed view says so; the casual one-liners do not, because burdening
"3 people came today" with a timezone caveat serves nobody.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

from bot_api.config import get_settings

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"

# Cloudflare keeps Web Analytics data for a rolling window. Asking for "this year" in
# December would otherwise silently return a partial answer presented as a whole one.
MAX_LOOKBACK_DAYS = 180


class AnalyticsUnavailable(Exception):
    """The numbers could not be read. Never shown raw -- callers phrase it for the owner."""


# --------------------------------------------------------------- what they asked about

# Words that mean "people arriving at my website" to someone who has never used an
# analytics dashboard. Deliberately generous: this only decides that the question is about
# traffic, and the cost of a false positive is a visitor summary, which is a reasonable
# thing to be handed by mistake.
_TRAFFIC_WORDS = re.compile(
    r"\b(?:visit|visits|visited|visitor|visitors|traffic|views?|viewed|"
    r"page ?views?|hits?|audience|analytics|stats?|statistics|"
    r"how many people|anyone (?:look|looking|looked|seen|see|visit|visiting|visited)|"
    r"people (?:look|looking|looked|seen|see|visit|visiting|visited|coming|came)|"
    r"seen (?:my|the) (?:site|website|page))\b",
    re.IGNORECASE,
)

# "Where does my traffic come from" is the second question every owner asks, and it can be
# phrased without any of the words above.
_SOURCE_WORDS = re.compile(
    r"\bwhere\b[^.?!]{0,30}\b(?:from|come|coming|comes)\b"
    r"|\b(?:referrer|referrers|referral|referrals|sources?)\b"
    r"|\bwho(?:'s| is| are)?\s+(?:sending|finding)\b"
    r"|\b(?:google|facebook|instagram|whatsapp)\b[^.?!]{0,20}\b(?:sending|bringing|traffic)\b",
    re.IGNORECASE,
)

# Tied to a popularity word rather than to "page" alone. "Which page is most popular" is a
# traffic question; "which page has my phone number on it" is a question about the site's
# contents, and answering that with a visitor chart would be a non-sequitur.
_PAGE_WORDS = re.compile(
    r"\b(?:most|popular|best|top|busiest)\b[^.?!]{0,25}\bpages?\b"
    r"|\bpages?\b[^.?!]{0,25}\b(?:most|popular|best|top|busiest|visited|viewed|looked at)\b",
    re.IGNORECASE,
)

_DEVICE_WORDS = re.compile(
    r"\b(?:mobile|phones?|desktop|computers?|laptops?|tablets?|ipad)\b"
    r"|\bwhat (?:are they|do they) (?:using|use)\b",
    re.IGNORECASE,
)

_PLACE_WORDS = re.compile(
    r"\b(?:countr(?:y|ies)|where are (?:they|my visitors|people)|which cit|abroad|"
    r"local|overseas|nationalit)\b",
    re.IGNORECASE,
)

# "add a visitor counter to my page" and "make the views bigger" are edits that happen to
# contain traffic words. Requiring the message not to be an instruction keeps them out.
_INSTRUCTION_RE = re.compile(
    r"^\s*(?:add|put|make|change|remove|delete|set|move|create|show me a|give me a|"
    r"insert|replace|update|write)\b",
    re.IGNORECASE,
)


def _looks_like_an_instruction(text: str) -> bool:
    return bool(_INSTRUCTION_RE.match(text)) and not text.rstrip().endswith("?")


def looks_like_a_traffic_question(text: str) -> bool:
    """Is this owner asking about who visited their website?

    Checked before the edit pipeline reads the message with a model, so a question about
    visitors never costs anything and is never mistaken for a request to change the site.
    """
    body = (text or "").strip()
    if not body or _looks_like_an_instruction(body):
        return False
    return bool(
        _TRAFFIC_WORDS.search(body)
        or _SOURCE_WORDS.search(body)
        # "Which page is most popular?" names no visitors and asks about nothing else.
        or _PAGE_WORDS.search(body)
    )


# Which slice of the numbers the question is really after. A question can be about several
# at once ("where are people coming from and what are they looking at"), so this returns a
# set rather than picking one.
ASPECT_TOTAL = "total"
ASPECT_SOURCES = "sources"
ASPECT_PLACES = "places"
ASPECT_PAGES = "pages"
ASPECT_DEVICES = "devices"


def aspects_for(text: str) -> set[str]:
    """The parts of the answer this question is asking for. Never empty."""
    body = text or ""
    found = set()
    if _SOURCE_WORDS.search(body):
        found.add(ASPECT_SOURCES)
    if _PLACE_WORDS.search(body):
        found.add(ASPECT_PLACES)
    if _PAGE_WORDS.search(body):
        found.add(ASPECT_PAGES)
    if _DEVICE_WORDS.search(body):
        found.add(ASPECT_DEVICES)
    # "Where do my visitors come from" is asked about referrers and about countries in the
    # same breath, and which was meant is not recoverable from the words. Both go in the
    # same reply, which costs nothing extra and is what they wanted either way.
    if ASPECT_SOURCES in found:
        found.add(ASPECT_PLACES)
    return found or {ASPECT_TOTAL}


# --------------------------------------------------------------- which stretch of time

@dataclass(frozen=True)
class Window:
    """A stretch of days to count, and what to call it in a sentence."""

    label: str           # "today", "this month" -- reads naturally mid-sentence
    start: datetime      # inclusive, UTC
    end: datetime        # exclusive, UTC
    previous_label: str  # what the period before this one is called, for comparisons

    @property
    def days(self) -> int:
        return max((self.end - self.start).days, 1)

    @property
    def is_single_day(self) -> bool:
        return self.days == 1


def _day(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


_LAST_N_DAYS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s+days?\b", re.IGNORECASE)
_LAST_N_WEEKS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,2})\s+weeks?\b", re.IGNORECASE)
_LAST_N_MONTHS = re.compile(r"\b(?:last|past|previous)\s+(\d{1,2})\s+months?\b", re.IGNORECASE)


def parse_window(text: str, now: datetime | None = None) -> Window | None:
    """The period the question is about, or None if it did not name one.

    Weeks start on Monday, because that is what "this week" means to someone running a
    shop. Returning None rather than guessing lets the caller pick a default suited to
    what was asked -- a week suits "how busy have I been", a month suits "where is my
    traffic coming from", and one number does not suit both.
    """
    body = (text or "").lower()
    now = now or datetime.now(timezone.utc)
    today = now.date()
    tomorrow = _day(today) + timedelta(days=1)

    if re.search(r"\b(?:today|so far today|this morning|right now|currently)\b", body):
        return Window("today", _day(today), tomorrow, "yesterday")

    if re.search(r"\byesterday\b", body):
        start = _day(today - timedelta(days=1))
        return Window("yesterday", start, start + timedelta(days=1), "the day before")

    match = _LAST_N_DAYS.search(body)
    if match:
        count = max(1, min(int(match.group(1)), MAX_LOOKBACK_DAYS))
        return Window(f"the last {count} days", _day(today) - timedelta(days=count - 1),
                      tomorrow, f"the {count} days before that")

    match = _LAST_N_WEEKS.search(body)
    if match:
        count = max(1, min(int(match.group(1)), 26))
        return Window(f"the last {count} weeks", _day(today) - timedelta(days=count * 7 - 1),
                      tomorrow, f"the {count} weeks before that")

    match = _LAST_N_MONTHS.search(body)
    if match:
        count = max(1, min(int(match.group(1)), 6))
        return Window(f"the last {count} months", _day(today) - timedelta(days=count * 30 - 1),
                      tomorrow, f"the {count} months before that")

    # "last week" and "this week" are different questions, and owners mean both literally:
    # "last week" is the completed Monday-to-Sunday, not the trailing seven days.
    if re.search(r"\b(?:last|previous)\s+week\b", body):
        this_monday = _day(today - timedelta(days=today.weekday()))
        return Window("last week", this_monday - timedelta(days=7), this_monday,
                      "the week before that")

    if re.search(r"\bthis\s+week\b|\bthe\s+week\b|\bweekly\b", body):
        return Window("this week", _day(today - timedelta(days=today.weekday())), tomorrow,
                      "last week")

    if re.search(r"\b(?:last|previous)\s+month\b", body):
        first_of_this_month = _day(today.replace(day=1))
        previous_first = (first_of_this_month - timedelta(days=1)).date().replace(day=1)
        return Window("last month", _day(previous_first), first_of_this_month,
                      "the month before that")

    if re.search(r"\bthis\s+month\b|\bthe\s+month\b|\bmonthly\b", body):
        return Window("this month", _day(today.replace(day=1)), tomorrow, "last month")

    if re.search(r"\b(?:this\s+year|last\s+year|so far this year|yearly|annual)\b", body):
        # Capped at the retention window rather than the calendar, so the number is one
        # Cloudflare can actually stand behind.
        start = max(_day(date(today.year, 1, 1)), _day(today) - timedelta(days=MAX_LOOKBACK_DAYS))
        return Window("this year so far", start, tomorrow, "before that")

    return None


def default_window(aspects: set[str], now: datetime | None = None) -> Window:
    """The period to use when the question named none.

    "How many visits?" means recently -- a week. "Where do they come from?" needs enough
    data to have a shape at all, so it gets a month.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    tomorrow = _day(today) + timedelta(days=1)
    if aspects == {ASPECT_TOTAL}:
        return Window("the last 7 days", _day(today) - timedelta(days=6), tomorrow,
                      "the 7 days before that")
    return Window("the last 30 days", _day(today) - timedelta(days=29), tomorrow,
                  "the 30 days before that")


# --------------------------------------------------------------- reading the numbers

# Validated against the live Cloudflare schema before this was written: every alias,
# dimension and orderBy below was accepted by api.cloudflare.com, so a failure at runtime
# is about permissions or data, never about the shape of the query.
_QUERY = """
query SiteTraffic(
  $accountTag: string,
  $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject,
  $trend: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject
) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      totals: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 1) {
        count
        sum { visits }
      }
      byDate: rumPageloadEventsAdaptiveGroups(filter: $trend, limit: 400, orderBy: [date_ASC]) {
        count
        sum { visits }
        dimensions { date }
      }
      byReferer: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 8, orderBy: [sum_visits_DESC]) {
        sum { visits }
        dimensions { refererHost }
      }
      byCountry: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 8, orderBy: [sum_visits_DESC]) {
        sum { visits }
        dimensions { countryName }
      }
      byPath: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 8, orderBy: [sum_visits_DESC]) {
        count
        sum { visits }
        dimensions { requestPath }
      }
      byDevice: rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 5, orderBy: [sum_visits_DESC]) {
        sum { visits }
        dimensions { deviceType }
      }
    }
  }
}
"""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter(site_tag: str, start: datetime, end: datetime) -> dict:
    return {
        "AND": [
            {"datetime_geq": _iso(start)},
            {"datetime_lt": _iso(end)},
            {"siteTag": site_tag},
            # 0 is "not a bot". Crawlers are most of the early traffic to a new
            # small-business site, and counting them would turn "nobody came yet" into
            # "twelve people came" -- the single most misleading thing this could say.
            {"bot": 0},
        ]
    }


async def fetch_traffic(site_tag: str, window: Window) -> dict:
    """Every number needed to answer any traffic question, in one round trip.

    The daily series covers the window *and the one before it*, so "up from last week" is
    free rather than a second query.
    """
    settings = get_settings()
    if not settings.cloudflare_api_token or not settings.cloudflare_account_id:
        raise AnalyticsUnavailable("cloudflare credentials are not configured")

    period = window.end - window.start
    variables = {
        "accountTag": settings.cloudflare_account_id,
        "filter": _filter(site_tag, window.start, window.end),
        "trend": _filter(site_tag, window.start - period, window.end),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                GRAPHQL_URL,
                headers={
                    "Authorization": f"Bearer {settings.cloudflare_api_token}",
                    "Content-Type": "application/json",
                },
                json={"query": _QUERY, "variables": variables},
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AnalyticsUnavailable(f"could not reach Cloudflare: {exc}") from exc

    if payload.get("errors"):
        reason = str(payload["errors"])[:300]
        logger.warning(
            "analytics.query_failed",
            extra={"event": "analytics.query_failed", "error": reason},
        )
        raise AnalyticsUnavailable(reason)

    accounts = ((payload.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        raise AnalyticsUnavailable("no analytics account in response")
    return _shape(accounts[0], window)


def _visits(row: dict) -> int:
    return int((row.get("sum") or {}).get("visits") or 0)


def _ranked(rows: list | None, dimension: str) -> list[tuple[str, int]]:
    ranked = []
    for row in rows or []:
        visits = _visits(row)
        if visits:
            ranked.append(((row.get("dimensions") or {}).get(dimension) or "", visits))
    return ranked


def _shape(account: dict, window: Window) -> dict:
    """Cloudflare's response, turned into the shape the renderers expect."""
    totals = account.get("totals") or []
    total_row = totals[0] if totals else {}

    window_start = window.start.date()
    current, previous = 0, 0
    per_day: list[tuple[date, int]] = []
    for row in account.get("byDate") or []:
        raw = (row.get("dimensions") or {}).get("date")
        if not raw:
            continue
        day = date.fromisoformat(raw)
        visits = _visits(row)
        if day >= window_start:
            current += visits
            per_day.append((day, visits))
        else:
            previous += visits

    return {
        "window": window,
        "pageviews": int(total_row.get("count") or 0),
        # Taken from the daily series rather than the totals block so both halves of a
        # comparison are always measured the same way.
        "visits": current,
        "visits_previous": previous,
        "per_day": per_day,
        "sources": _ranked(account.get("byReferer"), "refererHost"),
        "countries": _ranked(account.get("byCountry"), "countryName"),
        "pages": _ranked(account.get("byPath"), "requestPath"),
        "devices": _ranked(account.get("byDevice"), "deviceType"),
    }


# --------------------------------------------------------------- saying it in English

# Where a visitor came from, in words an owner recognises. Cloudflare reports the raw
# hostname, and "l.facebook.com" or "mail.google.com" means nothing to someone who runs a
# bakery -- what they want to know is "Facebook" and "Gmail".
_SOURCE_NAMES = {
    "google": "Google",
    "bing": "Bing",
    "duckduckgo": "DuckDuckGo",
    "yahoo": "Yahoo",
    "facebook": "Facebook",
    "instagram": "Instagram",
    "whatsapp": "WhatsApp",
    "t.co": "X (Twitter)",
    "twitter": "X (Twitter)",
    "x.com": "X (Twitter)",
    "linkedin": "LinkedIn",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "gmail": "Gmail",
    "mail.google": "Gmail",
    "outlook": "Outlook",
    "yelp": "Yelp",
    "tripadvisor": "TripAdvisor",
    "nextdoor": "Nextdoor",
    "telegram": "Telegram",
}

# What Cloudflare records when there was no referrer at all: the link was typed, tapped in
# a text message, or opened from an app that sends no referrer. To an owner this is the
# most important row on the list -- it is the customers they told themselves.
DIRECT_SOURCE = "Typed in, or tapped from a message"


def describe_source(host: str) -> str:
    """A referring hostname as an owner would name it."""
    cleaned = (host or "").strip().lower()
    if not cleaned:
        return DIRECT_SOURCE
    for needle, name in _SOURCE_NAMES.items():
        if needle in cleaned:
            return name
    # Something genuinely unknown -- another business's site, a local directory, a blog.
    # The bare domain is the most useful thing to show, so strip the noise around it.
    return cleaned.removeprefix("www.").removeprefix("m.")


_PAGE_NAMES = {
    "/": "Home page",
    "/index.html": "Home page",
    "/about.html": "About page",
    "/services.html": "Services page",
    "/contact.html": "Contact page",
}


def describe_page(path: str) -> str:
    cleaned = (path or "/").lower()
    return _PAGE_NAMES.get(cleaned, cleaned.strip("/") or "Home page")


_DEVICE_NAMES = {"desktop": "On a computer", "mobile": "On a phone", "tablet": "On a tablet"}


def describe_device(device: str) -> str:
    return _DEVICE_NAMES.get((device or "").lower(), "On something else")


def _people(count: int) -> str:
    """Visits, in the words an owner uses. "1 person", never "1 visits"."""
    return "1 person" if count == 1 else f"{count:,} people"


# Below this many visitors in the period being compared against, a percentage is noise
# wearing a suit. Three visitors becoming two is "down 33%" -- true, and read by the owner
# of a two-week-old site as proof that it is failing. Above it, the proportion starts to
# mean something and is the more useful way to say it.
MIN_VISITS_FOR_A_PERCENTAGE = 10


def _trend(data: dict) -> str:
    """The comparison sentence, or nothing when there is nothing worth comparing."""
    now, before = data["visits"], data["visits_previous"]
    label = data["window"].previous_label
    if before == 0:
        return f" That's up from none at all {label}." if now else ""
    if now == before:
        return f" Exactly the same as {label}."

    if before < MIN_VISITS_FOR_A_PERCENTAGE:
        # Small numbers are clearest said plainly. "Up from 3" is exactly as informative
        # as "up 33%" and cannot be misread as a trend.
        direction = "Up" if now > before else "Down"
        return f" {direction} from {before} {label}."

    change = round(abs(now - before) / before * 100)
    if change < 20:
        return f" About the same as {label} ({before})."
    if now > before:
        return f" That's up {change}% on {label} — worth knowing what you did differently."
    return f" That's down {change}% on {label}, when {_people(before)} came."


def _ranked_lines(ranked: list[tuple[str, int]], namer, limit: int = 5) -> list[str]:
    """The top few of a ranked dimension, merged by display name.

    Merging matters: Cloudflare reports google.com, www.google.com and google.co.uk as
    three rows, and an owner shown three Google lines learns less than one shown "Google".
    """
    merged: dict[str, int] = {}
    for value, visits in ranked:
        name = namer(value)
        merged[name] = merged.get(name, 0) + visits
    ordered = sorted(merged.items(), key=lambda pair: pair[1], reverse=True)
    return [f"  • {name} — {visits:,}" for name, visits in ordered[:limit]]


# --------------------------------------------------------------- the whole reply

def not_counting_yet(business_name: str, has_site: bool) -> str:
    """What to say when this site has no analytics identity at all.

    Reached by every site built before analytics existed, and by any site whose beacon
    could not be provisioned. Both are fixed the same way -- the next time the site is
    published -- so that is what it says, rather than explaining a system the owner never
    asked about.
    """
    if not has_site:
        return (
            "You haven't got a website yet, so there's nothing to count.\n\n"
            "Tell me about your business in one message and I'll build you one — then I "
            "can tell you exactly how many people visit it."
        )
    return (
        f"I'm not counting visitors to <b>{business_name}</b> yet — it went live before I "
        "could do that.\n\n"
        "Next time you change anything on it, I'll switch counting on, and from then I can "
        "tell you how many people came and where they found you. Want me to make a small "
        "change now to get that started?"
    )


def too_early(business_name: str, window_label: str, since: datetime) -> str:
    """When the whole window predates the day counting began.

    Without this, a site that started counting yesterday answers "how many visits last
    month?" with a truthful zero that reads as "nobody came" -- which is not what
    happened, and is the kind of wrong answer that makes an owner stop asking.
    """
    return (
        f"I can't tell you about {window_label} — I only started counting visitors to "
        f"<b>{business_name}</b> on {since:%d %B}.\n\n"
        "Ask me about today or this week and I'll give you the real numbers."
    )


def _nobody_came(window: Window, business_name: str, since: datetime | None) -> str:
    """Zero visits, said in a way that is honest without being demoralising."""
    if window.label == "today":
        opening = f"Nobody's visited <b>{business_name}</b> today yet."
    elif window.label == "yesterday":
        opening = f"No visits to <b>{business_name}</b> yesterday."
    else:
        opening = f"No visits to <b>{business_name}</b> in {window.label}."
    if since is not None and since.date() > window.start.date():
        opening += f" (I only started counting on {since:%d %B}.)"
    return (
        f"{opening}\n\n"
        "That's normal for a new site — people can only visit if they know it's there. "
        "The things that work quickest: send the link to customers you already have, put "
        "it on your Google business listing, and add it to your Facebook or Instagram "
        "page.\n\n"
        "Want me to check again later, or shall I help you get it in front of people?"
    )


def _sources_block(data: dict) -> list[str]:
    if not data["sources"]:
        return ["\n<b>Where they came from</b>\n  Nothing recorded for this period."]
    return ["\n<b>Where they came from</b>", *_ranked_lines(data["sources"], describe_source)]


def _places_block(data: dict) -> list[str]:
    if not data["countries"]:
        return []
    return ["\n<b>Where they are</b>", *_ranked_lines(data["countries"], lambda c: c or "Unknown")]


def _pages_block(data: dict) -> list[str]:
    if not data["pages"]:
        return []
    return ["\n<b>What they looked at</b>", *_ranked_lines(data["pages"], describe_page)]


def _devices_block(data: dict) -> list[str]:
    if not data["devices"]:
        return []
    return ["\n<b>How they viewed it</b>", *_ranked_lines(data["devices"], describe_device, limit=3)]


def _when(window: Window) -> str:
    """The period named so it reads as English mid-sentence.

    "in the last 30 days" is right and "in this month" is not, so the preposition follows
    the label rather than being bolted on to all of them.
    """
    return f"in {window.label}" if window.label.startswith("the ") else window.label


def render_traffic_answer(
    data: dict,
    aspects: set[str],
    business_name: str,
    since: datetime | None = None,
    extra: list[str] | None = None,
) -> str:
    """The numbers, written as a reply to whatever was actually asked.

    `aspects` decides what goes in beyond the headline: a question about where people came
    from gets sources and countries, a question about pages gets pages. The headline count
    is always there, because "where do they come from" is not worth answering without
    first saying how many "they" are.

    `extra` is for anything the caller wants said before the closing notes, so a footnote
    about when counting started is never stranded above the numbers it qualifies.
    """
    window = data["window"]
    visits = data["visits"]

    if visits == 0:
        return _nobody_came(window, business_name, since)

    lines = [
        f"<b>{_people(visits)}</b> visited <b>{business_name}</b> {_when(window)}." + _trend(data)
    ]

    # Pageviews only when they say something the visit count does not. "8 people, 30 pages"
    # means they are looking around; "8 people, 8 pages" means they are not, and printing
    # both numbers when they are the same is noise dressed up as detail.
    pageviews = data["pageviews"]
    if pageviews >= visits * 2:
        lines.append(
            f"\nBetween them they opened {pageviews:,} pages — so they're having a proper "
            "look around, not just glancing and leaving."
        )

    if ASPECT_SOURCES in aspects:
        lines.extend(_sources_block(data))
    if ASPECT_PLACES in aspects:
        lines.extend(_places_block(data))
    if ASPECT_PAGES in aspects:
        lines.extend(_pages_block(data))
    if ASPECT_DEVICES in aspects:
        lines.extend(_devices_block(data))

    if aspects == {ASPECT_TOTAL}:
        # Never a dead end: the owner now has a number and no idea what else they are
        # allowed to ask, which is the wall this bot's assistant exists to remove.
        lines.append(
            "\nAsk me <i>\"where did they come from?\"</i> or <i>\"which page did they "
            "look at?\"</i> and I'll tell you that too."
        )

    lines.extend(extra or [])

    if since is not None and since.date() > window.start.date():
        lines.append(f"\n<i>I only started counting on {since:%d %B}, so this covers from then.</i>")

    return "\n".join(lines)


def render_full_report(data: dict, business_name: str, since: datetime | None = None) -> str:
    """Everything known about a period, for someone who asked for everything.

    This is what /traffic prints. It carries the timezone note the conversational replies
    leave out: someone who asked for a report is reading rows of numbers and is owed the
    caveat, whereas someone who asked "how many today" is owed a sentence.
    """
    extra = []
    busiest = max(data["per_day"], key=lambda pair: pair[1], default=None)
    if busiest is not None and not data["window"].is_single_day and busiest[1] > 0:
        extra.append(f"\nBusiest day was {busiest[0]:%A %d %B}, with {_people(busiest[1])}.")
    extra.append("\n<i>Days are counted by UTC, so they may not match your clock exactly.</i>")

    return render_traffic_answer(
        data,
        {ASPECT_TOTAL, ASPECT_SOURCES, ASPECT_PLACES, ASPECT_PAGES, ASPECT_DEVICES},
        business_name,
        since,
        extra=extra,
    )


FAILED_REPLY = (
    "I couldn't get your visitor numbers just now — that's my end, not your site's, and "
    "your site is working fine.\n\n"
    "Try asking again in a few minutes. In the meantime, anything you'd like changed?"
)


# --------------------------------------------------------------- the one entry point

async def traffic_reply(business, text: str, full_report: bool = False) -> str | None:
    """The whole answer to a traffic question, or None if this was not one.

    Returning None rather than raising is what lets the bot's assistant try this first and
    fall through untouched: a message about anything else costs one regex and no network
    call. Everything that can go wrong past that point -- no beacon, no data yet, a
    Cloudflare outage -- comes back as a sentence for the owner, never as an exception,
    because a question about visitors failing loudly would take the whole conversation
    down with it.

    `business` is duck-typed on purpose: it needs a name, a site tag and an enabled-at
    date, which keeps this module free of any dependency on the ORM.
    """
    if not full_report and not looks_like_a_traffic_question(text):
        return None
    if business is None:
        # Which site is genuinely unknown here, and guessing would report one owner's
        # visitors as another's. The assistant's own reply asks them to pick.
        return None

    if not getattr(business, "cf_rum_site_tag", None):
        return not_counting_yet(business.name, has_site=True)

    aspects = (
        {ASPECT_TOTAL, ASPECT_SOURCES, ASPECT_PLACES, ASPECT_PAGES, ASPECT_DEVICES}
        if full_report
        else aspects_for(text)
    )
    window = parse_window(text) or default_window(aspects)

    since = getattr(business, "analytics_enabled_at", None)
    if since is not None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # Nothing in this window was ever watched, so a zero here would not mean "nobody
        # came" -- it would mean "nobody was counting", and the two must never be said
        # with the same words.
        if since >= window.end:
            return too_early(business.name, window.label, since)

    try:
        data = await fetch_traffic(business.cf_rum_site_tag, window)
    except AnalyticsUnavailable:
        return FAILED_REPLY

    if full_report:
        return render_full_report(data, business.name, since)
    return render_traffic_answer(data, aspects, business.name, since)
