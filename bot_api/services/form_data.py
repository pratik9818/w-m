"""Answering "give me my site data" -- reading back what customers actually sent.

The forms in worker/codegen/forms.py collect enquiries; the edge function beside them
stores each one and pings the owner as it lands. This is the other half: the owner who was
away from their phone, or who wants last week's again, or who simply wants to see the lot
in one place.

It follows analytics.py deliberately closely, for the same three reasons:

  1. **Nobody types /data.** They type "give me my site data", "any messages?", "who
     contacted me this week". So the parsing is of ordinary English and the slash command
     is a convenience for the few who like commands.
  2. **No model call.** These are rows in a table. Paying a language model to read them
     out loud would be paying for a worse version of a SELECT -- and, worse, for a version
     that can quietly get a phone number wrong. Every value below is printed exactly as
     the customer typed it.
  3. **Nothing is the same as no form.** "No enquiries this week" is a real, disappointing
     answer. "No enquiries this week" from a site with no form on it at all is a lie
     shaped like one, and the fix -- "say 'add a contact form'" -- is the thing the owner
     most needs to hear.

Times are shown in UTC, which is what the database records. For an owner in London that is
right or an hour out; further afield "today" can disagree with their clock by most of a
morning. The full report says so; the short answers do not, because burdening four
enquiries with a timezone footnote serves nobody.
"""
import html
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select

from bot_api.services.analytics import Window, parse_window
from bot_api.services.instructions import looks_like_an_instruction
from db.models import FormSubmission

logger = logging.getLogger(__name__)

# A chat message on a phone. Past this the reply stops being readable and starts being a
# spreadsheet, and Telegram refuses anything over 4096 characters anyway.
MAX_SHOWN = 8
MAX_VALUE_CHARS = 300
MAX_FIELDS_SHOWN = 8


# --------------------------------------------------------------- what they asked about

# The words an owner uses for "the things people sent me through my website". Generous on
# purpose: the cost of a false positive is being shown your own enquiries, which is a
# reasonable thing to be handed by mistake.
_DATA_WORDS = re.compile(
    r"\b(?:enquir(?:y|ies)|inquir(?:y|ies)|submissions?|responses?|"
    r"(?:my|the|site|website|form)\s+data|form\s+(?:entries|fills|results)|"
    r"leads?|contact\s+form\s+(?:data|messages?|entries)|"
    r"messages?\s+(?:from|through|via)\s+(?:my|the)\s+(?:site|website|form)|"
    r"who\s+(?:has\s+)?(?:contacted|messaged|written to|emailed)\s+me|"
    r"anyone\s+(?:contacted|messaged|written|filled|got in touch)|"
    r"(?:did|has)\s+anyone\s+(?:fill|filled|send|sent|write|written|contact))\b",
    re.IGNORECASE,
)

def looks_like_a_data_question(text: str) -> bool:
    """Is this owner asking to see what customers sent them?

    Checked before the edit pipeline reads the message with a model, so asking for your own
    data never costs anything and is never mistaken for a request to change the site.

    "add a contact form", "remove the enquiry form" and "make the message box bigger" all
    contain the words above and none of them is a request to see the data, so an
    instruction is refused here before those words are looked for at all.
    """
    body = (text or "").strip()
    if not body or looks_like_an_instruction(body):
        return False
    return bool(_DATA_WORDS.search(body))


# --------------------------------------------------------------- reading the rows

async def recent_submissions(
    session, business_id, window: Window | None, limit: int = MAX_SHOWN
) -> tuple[list[FormSubmission], int]:
    """The newest enquiries in `window` (or ever), and how many there are in total.

    The total is counted rather than inferred from the list: "showing 8 of 63" is the
    difference between a reply that answers the question and one that quietly hides
    fifty-five enquiries.
    """
    filters = [FormSubmission.business_id == business_id]
    if window is not None:
        filters.append(FormSubmission.submitted_at >= window.start)
        filters.append(FormSubmission.submitted_at < window.end)

    total = await session.scalar(
        select(func.count(FormSubmission.id)).where(*filters)
    )
    rows = await session.execute(
        select(FormSubmission)
        .where(*filters)
        .order_by(FormSubmission.submitted_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all()), int(total or 0)


async def submission_counts(session, business_ids: list) -> dict:
    """How many enquiries each of these sites has taken, ever. One grouped query."""
    if not business_ids:
        return {}
    rows = await session.execute(
        select(FormSubmission.business_id, func.count(FormSubmission.id))
        .where(FormSubmission.business_id.in_(business_ids))
        .group_by(FormSubmission.business_id)
    )
    return {business_id: count for business_id, count in rows.all()}


# --------------------------------------------------------------- saying it back

def _when(moment: datetime, now: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = (now.date() - moment.date()).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return f"today, {clock}"
    if days == 1:
        return f"yesterday, {clock}"
    # Built by hand rather than with strftime("%-d"): that is a glibc extension which
    # raises on Windows, where this bot is developed, and "05 Aug" reads like a form field.
    return f"{moment.day} {moment.strftime('%b')}, {clock}"


def _labels_for(business, form_name: str) -> dict:
    """The owner's own wording for each field, so a reply reads like their form."""
    definition = (getattr(business, "forms", None) or {}).get(form_name) or {}
    return {
        field.get("name"): field.get("label")
        for field in definition.get("fields") or []
        if field.get("name")
    }


def render_submission(business, row: FormSubmission, index: int, now: datetime) -> str:
    labels = _labels_for(business, row.form_name)
    lines = [f"<b>{index}.</b> {_when(row.submitted_at, now)}"]
    for shown, (key, value) in enumerate((row.payload or {}).items()):
        if shown >= MAX_FIELDS_SHOWN:
            break
        text = str(value or "").strip()
        if not text:
            continue
        if len(text) > MAX_VALUE_CHARS:
            text = text[:MAX_VALUE_CHARS].rstrip() + "…"
        label = labels.get(key) or key.replace("_", " ")
        lines.append(f"   <b>{html.escape(str(label))}:</b> {html.escape(text)}")
    return "\n".join(lines)


def no_form_yet(business_name: str) -> str:
    return (
        f"<b>{business_name}</b> doesn't have an enquiry form yet, so there's nothing "
        "coming in to show you.\n\n"
        'Say <i>"add a contact form"</i> and I\'ll put one on your contact page — name, '
        "email and message by default, or tell me exactly which details you want asked "
        "for. Every message sent through it lands in this chat straight away."
    )


def nothing_yet(business_name: str, period: str | None) -> str:
    when = f" {period}" if period else " yet"
    return (
        f"No enquiries{when} for <b>{business_name}</b>.\n\n"
        "Your form is live and working — nobody has filled it in. Every one that comes "
        "in lands in this chat the moment it's sent, so you don't have to keep checking."
    )


async def submissions_reply(session, business, text: str, full_report: bool = False) -> str | None:
    """The whole answer to "give me my site data", or None if that is not what was asked.

    Returning None rather than raising is what lets the assistant try this first and fall
    through untouched: a message about anything else costs one regex and no query.

    `business` is the site the owner is currently working on. With none picked, this
    returns None and the assistant's own reply asks them which -- guessing would show one
    owner's customers under another site's name.
    """
    if not full_report and not looks_like_a_data_question(text):
        return None
    if business is None:
        return None

    if not (getattr(business, "forms", None) or {}):
        return no_form_yet(business.name)

    window = parse_window(text)
    now = datetime.now(timezone.utc)
    rows, total = await recent_submissions(session, business.id, window)

    if not rows:
        return nothing_yet(business.name, window.label if window else None)

    period = f" {window.label}" if window else ""
    plural = "enquiry" if total == 1 else "enquiries"
    header = f"📬 <b>{business.name}</b> — {total} {plural}{period}"

    body = "\n\n".join(
        render_submission(business, row, index, now)
        for index, row in enumerate(rows, start=1)
    )

    footer = []
    if total > len(rows):
        footer.append(
            f"Showing the {len(rows)} most recent. Ask for a stretch of time — "
            '<i>"enquiries this week"</i>, <i>"last month"</i> — to see others.'
        )
    if full_report:
        footer.append("Times are UTC.")

    parts = [header, body, *footer]
    return "\n\n".join(parts)
