"""A form that looks right and sends nothing is the failure this feature can produce.

Nothing else on a generated site fails silently: a broken layout is visible, a wrong price
is visible, a dead link is visible the moment somebody clicks it. A form that posts into
the void looks exactly like one that works -- to the owner, to the visitor who typed three
sentences into it, and to every check we run. The enquiry simply never exists.

So the cases below are mostly about that one risk: the form is never put on a page unless
there is somewhere for it to post, it goes back on after a rebuild that rewrote every page,
and it comes off cleanly when it is meant to.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from bot_api.services.form_data import (
    looks_like_a_data_question,
    no_form_yet,
    nothing_yet,
    render_submission,
    submissions_reply,
)
from worker.codegen.forms import (
    HONEYPOT_FIELD,
    MAX_FIELDS,
    FormRejected,
    apply_forms,
    build_definition,
    describe_form,
    default_fields,
    page_for,
)
from worker.codegen.html_check import html_problems
from worker.codegen.js_check import script_problems

ENDPOINT = "https://example.supabase.co/functions/v1/site-form"
KEY = "a-form-key"

PAGE = """<!doctype html>
<html lang="en"><head><title>Contact</title></head>
<body>
  <header class="site-header"><a class="logo">Rise &amp; Crumb</a></header>
<main>
  <section class="section">
    <div class="container"><h2 class="section-title">Find us</h2></div>
  </section>
  <section class="cta-band">
    <div class="container"><h2 class="cta-title">Come and say hello</h2></div>
  </section>
</main>
  <footer class="site-footer"><p class="footer-note">&copy; 2026</p></footer>
</body>
</html>
"""

FILES = {
    "index.html": PAGE.replace("Contact", "Home"),
    "contact.html": PAGE,
    "style.css": "/* ---- generated ---- */\n.hero { color: red; }\n",
}


def _applied(op=None, files=None, key=KEY, endpoint=ENDPOINT, layout="multipage"):
    name, form = build_definition(op or {}, layout)
    return name, form, apply_forms(files or FILES, {name: form}, key, endpoint)


# --------------------------------------------------------------- defining a form

def test_asking_for_a_form_without_saying_what_it_asks_gets_the_obvious_three():
    """"Add a contact form" is the whole request most owners make."""
    _, form = build_definition({}, "multipage")
    assert [field["name"] for field in form["fields"]] == ["name", "email", "message"]
    assert form["page"] == "contact.html"


def test_the_kind_of_box_is_worked_out_from_the_owners_own_words():
    """An owner says "phone", never "tel". Making them say it twice is not an option."""
    _, form = build_definition(
        {"fields": ["Your name", "Email address", "Phone", "Which date", "How many guests",
                    "Tell us more"]},
        "multipage",
    )
    assert [field["type"] for field in form["fields"]] == [
        "text", "email", "tel", "date", "number", "textarea"
    ]


def test_field_names_come_from_the_labels_and_never_collide():
    """The name is what the owner reads back later, so "field_2" is not an answer."""
    _, form = build_definition({"fields": ["Your name", "Your name", "Party size"]},
                               "multipage")
    assert [field["name"] for field in form["fields"]] == ["your_name", "your_name_2",
                                                           "party_size"]


def test_a_field_called_website_is_renamed_rather_than_silently_discarded():
    """It shares its name with the honeypot, and the endpoint drops that field on arrival.

    A "Website" box on a form for a design agency is an entirely ordinary thing to want,
    and left alone every answer to it would vanish between the page and the table.
    """
    _, form = build_definition({"fields": ["Website"]}, "multipage")
    assert form["fields"][0]["name"] != HONEYPOT_FIELD


def test_a_form_longer_than_anyone_would_fill_in_is_cut_to_the_ceiling():
    _, form = build_definition({"fields": [f"Question {n}" for n in range(40)]}, "multipage")
    assert len(form["fields"]) == MAX_FIELDS


def test_fields_that_are_all_blank_are_refused_rather_than_built_empty():
    with pytest.raises(FormRejected):
        build_definition({"fields": ["", "   ", {}]}, "multipage")


def test_a_landing_site_puts_every_form_on_its_only_page():
    """There is no contact.html there; honouring "the contact page" literally writes to a
    file that does not exist, and the form is silently never seen again."""
    assert page_for("contact", "landing") == "index.html"
    assert page_for("services", "landing") == "index.html"
    assert page_for("services", "multipage") == "services.html"
    assert page_for("somewhere nobody has", "multipage") == "contact.html"


def test_the_form_is_described_in_the_owners_words_for_the_confirmation():
    _, form = build_definition({"fields": ["Your name", "Phone", "Message"]}, "multipage")
    described = describe_form(form)
    assert "contact page" in described
    assert "your name, phone and message" in described


# --------------------------------------------------------------- what lands on the page

def test_the_page_is_still_well_formed_and_the_script_still_parses():
    """Both are build-failing checks. A form that fails them takes the whole site down."""
    _, _, files = _applied({"fields": ["Your name", "Email", "Message"]})
    assert html_problems(files["contact.html"]) == []
    assert script_problems(files["contact.html"]) == []


def test_the_owners_wording_is_escaped_before_it_reaches_the_page():
    """The title and labels are the owner's free text, and they go into markup."""
    _, _, files = _applied({"title": 'Book a "table" <now>', "fields": ["Name & number"]})
    page = files["contact.html"]
    assert '<now>' not in page
    assert "Name & number" not in page
    assert "&amp;" in page and "&lt;now&gt;" in page


def test_the_form_carries_a_honeypot_and_it_is_never_required():
    _, _, files = _applied()
    page = files["contact.html"]
    assert f'name="{HONEYPOT_FIELD}"' in page
    trap = page[page.index('class="form-trap"'):page.index("form-submit")]
    assert "required" not in trap


def test_only_the_page_it_belongs_to_is_touched():
    """Every other file must come back byte-identical -- that is what a patch promises."""
    _, _, files = _applied()
    assert files["index.html"] == FILES["index.html"]
    assert files["contact.html"] != FILES["contact.html"]


def test_the_form_sits_above_the_closing_call_to_action():
    """A page that ends with a text box has stopped asking for the sale."""
    _, _, files = _applied()
    page = files["contact.html"]
    assert page.index("<!-- form:contact -->") < page.index('class="cta-band"')


def test_the_stylesheet_learns_the_form_classes_exactly_once():
    """A form added to a site built last week meets a stylesheet that has never heard of
    it, and an unstyled form is also counted as stylesheet drift by the build."""
    _, form, files = _applied()
    assert ".site-form {" in files["style.css"]
    twice = apply_forms(files, {"contact": form}, KEY, ENDPOINT)
    assert twice["style.css"].count(".site-form {") == 1


def test_applying_the_same_form_again_changes_nothing():
    """It runs on every build, and a build whose bytes moved for no reason is reported to
    the owner as a change they never asked for."""
    name, form, files = _applied()
    assert apply_forms(files, {name: form}, KEY, ENDPOINT) == files


def test_removing_the_form_puts_the_pages_back_exactly_as_they_were():
    _, _, files = _applied()
    assert apply_forms(files, {}, KEY, ENDPOINT) == FILES


def test_a_form_whose_definition_is_gone_is_stripped_even_though_nothing_names_it():
    """Stripping keyed on the current definitions would leave a deleted form on the page
    for ever -- still posting, with nothing left that knows it is there."""
    _, _, files = _applied({"form": "booking"})
    stripped = apply_forms(files, {}, KEY, ENDPOINT)
    assert "<!-- form:booking -->" not in stripped["contact.html"]
    assert "form-script" not in stripped["contact.html"]


def test_moving_a_form_to_another_page_does_not_leave_a_copy_behind():
    name, form, files = _applied()
    moved = dict(form, page="index.html")
    files = apply_forms(files, {name: moved}, KEY, ENDPOINT)
    assert "<!-- form:contact -->" in files["index.html"]
    assert "<!-- form:contact -->" not in files["contact.html"]
    assert "form-script" not in files["contact.html"]


def test_a_rebuild_that_rewrote_every_page_gets_the_form_back():
    """The definition lives on the business, not in the markup, exactly so that a redesign
    -- which returns four brand-new pages -- cannot quietly drop it."""
    name, form, _ = _applied()
    rebuilt = dict(FILES, contact_html=None)
    rebuilt = {key: value for key, value in FILES.items()}
    after = apply_forms(rebuilt, {name: form}, KEY, ENDPOINT)
    assert "<!-- form:contact -->" in after["contact.html"]


def test_two_forms_on_one_page_share_a_single_script():
    _, contact = build_definition({"form": "contact"}, "multipage")
    _, booking = build_definition({"form": "booking", "fields": ["Which date"]}, "multipage")
    files = apply_forms(FILES, {"contact": contact, "booking": booking}, KEY, ENDPOINT)
    assert files["contact.html"].count("<!-- form-script -->") == 1
    assert script_problems(files["contact.html"]) == []


@pytest.mark.parametrize("key, endpoint", [(None, ENDPOINT), (KEY, ""), (None, "")])
def test_no_endpoint_means_no_form_at_all(key, endpoint):
    """The whole point. A form with nowhere to post looks like it works and loses every
    message sent through it, so not shipping one is the only safe failure."""
    name, form = build_definition({}, "multipage")
    files = apply_forms(FILES, {name: form}, key, endpoint)
    assert files == FILES


def test_the_endpoint_and_key_cannot_break_out_of_the_script():
    """Both are interpolated into JavaScript, and one of them is generated."""
    _, _, files = _applied(endpoint='https://x/"+alert(1)+"', key='a"</script><script>x')
    assert script_problems(files["contact.html"]) == []
    assert "</script><script>" not in files["contact.html"].split("<!-- form-script -->")[1]


# --------------------------------------------------------------- asking for the data

@pytest.mark.parametrize("question", [
    "give me my site data",
    "show me my site data",
    "any enquiries?",
    "how many enquiries did i get this week",
    "who contacted me",
    "has anyone filled in the form",
    "did anyone send anything",
    "show me my leads",
    "messages from my website please",
])
def test_asking_for_the_data_is_recognised(question):
    assert looks_like_a_data_question(question)


@pytest.mark.parametrize("message", [
    "add a contact form",
    "put an enquiry form on the about page",
    "make the message box bigger",
    "remove the form",
    "change my hours to 9-6",
    "how many people visited today",
    "where are my visitors coming from",
])
def test_a_change_to_the_form_is_not_a_request_to_read_it(message):
    """Both are about a form and only one of them is a question. Getting this wrong sends
    an owner asking for a form a list of enquiries they have not had yet."""
    assert not looks_like_a_data_question(message)


class _Row:
    def __init__(self, payload, submitted_at, form_name="contact"):
        self.payload = payload
        self.submitted_at = submitted_at
        self.form_name = form_name


class _Business:
    id = "b1"
    name = "Rise & Crumb"
    forms = {"contact": {"page": "contact.html", "fields": [
        {"name": "your_name", "label": "Your name"},
        {"name": "message", "label": "Message"},
    ]}}


def test_an_enquiry_is_shown_under_the_labels_the_owner_chose():
    now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
    row = _Row({"your_name": "Ravi", "message": "Do you cater weddings?"},
               now - timedelta(hours=3))
    rendered = render_submission(_Business(), row, 1, now)
    assert "Your name:</b> Ravi" in rendered
    assert "today, 15:00" in rendered


def test_a_customers_words_are_escaped_before_they_reach_the_chat():
    """The payload is written by a stranger on the internet and sent to the owner as HTML."""
    now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
    row = _Row({"message": "<b>hi</b> & bye"}, now)
    assert "&lt;b&gt;hi&lt;/b&gt; &amp; bye" in render_submission(_Business(), row, 1, now)


def test_a_long_message_is_cut_rather_than_lost_to_telegrams_limit():
    now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
    row = _Row({"message": "x" * 5000}, now)
    assert len(render_submission(_Business(), row, 1, now)) < 500


class _StubSession:
    """Stands in for the database: the queries here are two lines and the wiring is not."""

    def __init__(self, rows):
        self.rows = rows

    async def scalar(self, _query):
        return len(self.rows)

    async def execute(self, _query):
        rows = self.rows

        class Result:
            def scalars(self):
                class Scalars:
                    def all(self):
                        return rows
                return Scalars()

        return Result()


class _FormlessBusiness(_Business):
    forms = {}


@pytest.mark.asyncio
async def test_a_site_with_no_form_is_told_that_and_not_that_nobody_wrote():
    """"No enquiries" from a site with no form on it is a lie shaped like an answer, and
    the thing the owner most needs to hear is how to get one."""
    reply = await submissions_reply(_StubSession([]), _FormlessBusiness(), "give me my data")
    assert reply == no_form_yet("Rise & Crumb")
    assert "add a contact form" in reply


@pytest.mark.asyncio
async def test_a_form_nobody_has_used_says_so_plainly():
    reply = await submissions_reply(_StubSession([]), _Business(), "any enquiries?")
    assert reply == nothing_yet("Rise & Crumb", None)


@pytest.mark.asyncio
async def test_the_period_the_owner_named_is_carried_into_the_answer():
    reply = await submissions_reply(_StubSession([]), _Business(), "any enquiries last week?")
    assert "last week" in reply


@pytest.mark.asyncio
async def test_a_message_about_anything_else_is_left_alone():
    """Returning None is what lets the assistant fall through untouched -- and what keeps
    "make the heading bigger" from being answered with a list of enquiries."""
    assert await submissions_reply(_StubSession([]), _Business(), "make it green") is None


@pytest.mark.asyncio
async def test_with_no_site_chosen_it_declines_rather_than_guessing():
    """Showing one owner's customers under another site's name is worse than no answer."""
    assert await submissions_reply(_StubSession([]), None, "give me my site data") is None


@pytest.mark.asyncio
async def test_the_total_is_reported_even_when_only_a_few_are_shown():
    now = datetime.now(timezone.utc)
    rows = [_Row({"message": f"number {n}"}, now) for n in range(8)]

    class ManySession(_StubSession):
        async def scalar(self, _query):
            return 63

    reply = await submissions_reply(ManySession(rows), _Business(), "give me my site data")
    assert "63 enquiries" in reply
    assert "8 most recent" in reply


# --------------------------------------------------------------- the parser's view

def test_the_edge_function_and_the_page_agree_on_the_honeypot():
    """They are two files in two languages and the check is one string. If they disagree,
    every real submission is silently binned as spam."""
    source = open("supabase/functions/site-form/index.ts", encoding="utf-8").read()
    assert f'HONEYPOT_FIELD = "{HONEYPOT_FIELD}"' in source


def test_the_parser_is_told_forms_are_possible_and_never_to_write_one():
    """The prompts said the opposite for months, and a model that writes its own form
    produces one that posts nowhere."""
    from bot_api.services.nl_edit import PROMPT_TEMPLATE, TOOLS

    names = {tool["name"] for tool in TOOLS}
    assert {"add_form", "remove_form"} <= names
    assert "Never write a form yourself" in PROMPT_TEMPLATE


def test_every_page_prompt_forbids_the_model_writing_a_form():
    for name in ("_contract.md", "patch.md", "repair.md"):
        source = open(f"worker/codegen/prompts/{name}", encoding="utf-8").read()
        assert "form" in source.lower()
        assert "never" in source.lower()


def test_default_fields_are_the_three_everyone_means():
    assert json.dumps(default_fields())  # serialisable: it is stored as JSONB
    assert [field["required"] for field in default_fields()] == [True, True, True]
