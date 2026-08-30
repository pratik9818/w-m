"""Put a working enquiry form on a generated site, without asking a model to write one.

Until now every site ended the same way: a phone number and an email address. That serves
the visitor who is ready to ring a stranger and loses everyone else -- the one browsing at
eleven at night, the one who wants to send three sentences and get on with their evening.
The prompts said so outright ("no `<form>` elements: there is no server to receive a
submission"), which was true, and is what this module and the edge function beside it
change.

The form is built here, in code, for the same reason `style_ops.py` edits declarations in
code: a form is the one thing on a site where "it looked fine" and "it worked" come apart.
A model writing markup can produce a form that renders beautifully, posts nowhere, and
loses every message a customer sends -- and nobody finds out, because a lost enquiry
leaves no trace anywhere. Nothing about that is worth a model call. So:

  - The markup, the script and the styling are generated from a definition, identically
    every time, and the definition is the thing the owner's request is turned into.
  - Injection is idempotent, keyed on a marker comment. It runs on every build, so a
    rebuild that rewrites all four pages from scratch puts the form back rather than
    silently dropping it -- which is exactly how "add a contact form" would otherwise
    survive for precisely as long as it took the owner to ask for something else.
  - Removing a form is the same operation with the definition gone, so the two paths
    cannot drift apart.

The fields are the owner's. "Just a name and a message" and a nine-field intake form are
both ordinary requests; where they say nothing, they get name/email/message, which is what
a contact form means to almost everybody.
"""
from __future__ import annotations

import html
import re
import secrets

from bot_api.config import get_settings

DEFAULT_FORM_NAME = "contact"
# The edge function that receives a submission. See supabase/functions/site-form/.
FUNCTION_PATH = "/functions/v1/site-form"
STYLESHEET_FILE = "style.css"

# A form long enough to need scrolling on a phone is a form nobody finishes. This is a
# ceiling on what an owner can ask for, not a target.
MAX_FIELDS = 12
MAX_LABEL_CHARS = 60
MAX_TITLE_CHARS = 80
MAX_BUTTON_CHARS = 40
MAX_MESSAGE_CHARS = 200
MAX_FORM_NAME_CHARS = 40

# Matched against what the browser actually implements. An invented type ("phone") renders
# as a plain text box on some browsers and is refused outright by others, and either way
# the owner asked for something they did not get.
FIELD_TYPES = frozenset({"text", "email", "tel", "textarea", "number", "date"})

# What a field's wording implies about the box it should be. The owner says "phone", not
# "tel", and asking a model to translate that is paying for a lookup table.
_TYPE_HINTS = (
    (re.compile(r"\b(?:e-?mail)\b", re.IGNORECASE), "email"),
    (re.compile(r"\b(?:phone|mobile|telephone|contact number|whatsapp)\b", re.IGNORECASE), "tel"),
    (re.compile(r"\b(?:message|details|enquiry|inquiry|comments?|notes?|"
                r"description|requirements?|question)\b"
                r"|\b(?:tell|ask) (?:us|me)\b|\banything else\b|\bhow can we help\b",
                re.IGNORECASE), "textarea"),
    (re.compile(r"\b(?:date|day|when)\b", re.IGNORECASE), "date"),
    (re.compile(r"\b(?:how many|number of|quantity|guests|people|party size)\b",
                re.IGNORECASE), "number"),
)

# The pages a form can live on, by the word an owner would use for each.
_PAGE_WORDS = {
    "contact": "contact.html", "contact us": "contact.html", "contact page": "contact.html",
    "home": "index.html", "homepage": "index.html", "home page": "index.html",
    "index": "index.html", "front": "index.html", "landing": "index.html",
    "about": "about.html", "about us": "about.html",
    "services": "services.html", "service": "services.html",
}
DEFAULT_PAGE = "contact.html"

DEFAULT_SUCCESS_MESSAGE = "Thanks — we've got your message and will be in touch shortly."
DEFAULT_SUBMIT_LABEL = "Send message"
DEFAULT_TITLE = "Send us a message"

# The honeypot. Named for what a bot expects to find rather than for what it is: a field
# called "leave-blank" is skipped by anything smarter than a form-filler, and a field
# called "website" is filled in by most of them. Never shown, never required, and its
# presence in the payload is what the edge function refuses on.
HONEYPOT_FIELD = "website"


class FormRejected(Exception):
    """The form asked for could not be built. The message is shown to the owner."""


# --------------------------------------------------------------- defining a form

def _slug(label: str, taken: set[str]) -> str:
    """A field name derived from its label, unique within the form.

    The name is what the payload is keyed by and what the owner sees when they ask for
    their data back, so it is derived from their own wording rather than assigned -- a
    column headed "party_size" is readable; one headed "field_3" is not.
    """
    base = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")[:32] or "field"
    if base == HONEYPOT_FIELD:
        # Would be discarded as spam by the endpoint on arrival. Rare enough to rename
        # silently, and fatal enough that it must never be stored.
        base = f"{base}_url"
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"[:32]
        suffix += 1
    return name


def _type_for(label: str, given: str | None) -> str:
    given = (given or "").strip().lower()
    if given in FIELD_TYPES:
        return given
    for pattern, kind in _TYPE_HINTS:
        if pattern.search(label):
            return kind
    return "text"


def normalise_fields(raw_fields) -> list[dict]:
    """Turn whatever the parser produced into field definitions, or refuse.

    Accepts a list of strings ("name", "email") as readily as a list of objects, because
    both are what a model returns when asked for fields and neither is wrong.
    """
    if not raw_fields:
        return default_fields()
    if isinstance(raw_fields, (str, dict)):
        raw_fields = [raw_fields]

    fields: list[dict] = []
    taken: set[str] = set()
    for entry in raw_fields:
        if isinstance(entry, str):
            entry = {"label": entry}
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("name") or "").strip()
        label = re.sub(r"\s+", " ", label)[:MAX_LABEL_CHARS]
        if not label:
            continue
        name = _slug(str(entry.get("name") or label), taken)
        taken.add(name)
        fields.append({
            "name": name,
            "label": label,
            "type": _type_for(label, entry.get("type")),
            # Everything is optional unless it was asked for. A required field the visitor
            # cannot supply is a form they abandon, and the owner would rather have a name
            # and a phone number than nothing at all.
            "required": bool(entry.get("required", False)),
        })
        if len(fields) >= MAX_FIELDS:
            break

    if not fields:
        raise FormRejected("none of those fields had a name I could put on the form")
    return fields


def default_fields() -> list[dict]:
    """What a contact form means when nobody says otherwise."""
    return [
        {"name": "name", "label": "Your name", "type": "text", "required": True},
        {"name": "email", "label": "Email", "type": "email", "required": True},
        {"name": "message", "label": "Message", "type": "textarea", "required": True},
    ]


def page_for(requested: str | None, layout: str | None) -> str:
    """Which file the form goes in.

    A landing site has exactly one page, so every form goes on it whatever the owner calls
    the section they mean -- "put it on the contact page" is about the contact section
    there, and honouring it literally would mean writing to a file that does not exist.
    """
    if layout == "landing":
        return "index.html"
    wanted = re.sub(r"\s+", " ", (requested or "").strip().lower()).removesuffix(".html")
    return _PAGE_WORDS.get(wanted, DEFAULT_PAGE)


def _clean_text(value, fallback: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit] if text else fallback


def build_definition(op: dict, layout: str | None) -> tuple[str, dict]:
    """Turn a parsed `add_form` operation into (form name, stored definition)."""
    name = re.sub(r"[^a-z0-9_-]+", "-", str(op.get("form") or DEFAULT_FORM_NAME).strip().lower())
    name = name.strip("-")[:MAX_FORM_NAME_CHARS] or DEFAULT_FORM_NAME
    return name, {
        "title": _clean_text(op.get("title"), DEFAULT_TITLE, MAX_TITLE_CHARS),
        "intro": _clean_text(op.get("intro"), "", MAX_MESSAGE_CHARS),
        "submit_label": _clean_text(op.get("submit_label"), DEFAULT_SUBMIT_LABEL,
                                    MAX_BUTTON_CHARS),
        "success_message": _clean_text(op.get("success_message"), DEFAULT_SUCCESS_MESSAGE,
                                       MAX_MESSAGE_CHARS),
        "page": page_for(op.get("page"), layout),
        "fields": normalise_fields(op.get("fields")),
    }


def describe_form(form: dict) -> str:
    """The form in the owner's own words, for the confirmation question and the reply."""
    labels = [field["label"].lower() for field in form["fields"]]
    if len(labels) == 1:
        listed = labels[0]
    else:
        listed = ", ".join(labels[:-1]) + f" and {labels[-1]}"
    where = {
        "contact.html": "your contact page", "index.html": "your home page",
        "about.html": "your about page", "services.html": "your services page",
    }.get(form["page"], "your site")
    return f"a form on {where} asking for {listed}"


def new_form_key() -> str:
    """A fresh public identifier for a site's forms."""
    return secrets.token_urlsafe(24)[:48]


def form_endpoint() -> str:
    """The address a page posts an enquiry to, or "" if there isn't one configured.

    Empty is a real answer and callers must respect it: with no endpoint, `apply_forms`
    puts no form on any page. A form that renders and posts nowhere looks like it works,
    and loses every message sent through it without leaving a trace anywhere.
    """
    settings = get_settings()
    if settings.form_endpoint_url:
        return settings.form_endpoint_url.strip()
    if settings.supabase_url:
        return f"{settings.supabase_url.strip().rstrip('/')}{FUNCTION_PATH}"
    return ""


# --------------------------------------------------------------- rendering

def _marker(form_name: str) -> tuple[str, str]:
    return f"<!-- form:{form_name} -->", f"<!-- /form:{form_name} -->"


SCRIPT_OPEN = "<!-- form-script -->"
SCRIPT_CLOSE = "<!-- /form-script -->"
CSS_MARKER = "/* ---- enquiry form ---- */"
CSS_END_MARKER = "/* ---- end enquiry form ---- */"

# Matches any form block, whatever it is called. Deliberately not built per name: a form
# is also stripped when its definition is gone, and at that point its name is gone too --
# keying the strip on the current definitions would leave a deleted form on the page for
# ever, still posting, with nothing left that knows it is there.
_ANY_BLOCK_RE = re.compile(
    r"[ \t]*<!-- form:(?P<name>[\w-]{1,40}) -->.*?<!-- /form:(?P=name) -->[ \t]*\n?",
    re.DOTALL,
)
_SCRIPT_BLOCK_RE = re.compile(
    rf"[ \t]*{re.escape(SCRIPT_OPEN)}.*?{re.escape(SCRIPT_CLOSE)}[ \t]*\n?", re.DOTALL
)


def _field_html(form_name: str, field: dict) -> str:
    field_id = f"f-{form_name}-{field['name']}"
    label = html.escape(field["label"])
    required = " required" if field["required"] else ""
    # Marked for the visitor as well as for the browser. A required field that only
    # announces itself by refusing to submit is a form people give up on.
    star = ' <span class="form-required">*</span>' if field["required"] else ""
    if field["type"] == "textarea":
        control = (
            f'<textarea class="form-input form-textarea" id="{field_id}" '
            f'name="{field["name"]}" rows="5" maxlength="4000"{required}></textarea>'
        )
    else:
        control = (
            f'<input class="form-input" type="{field["type"]}" id="{field_id}" '
            f'name="{field["name"]}" maxlength="500"{required}>'
        )
    return (
        '      <div class="form-field">\n'
        f'        <label class="form-label" for="{field_id}">{label}{star}</label>\n'
        f"        {control}\n"
        "      </div>"
    )


def render_form(form_name: str, form: dict) -> str:
    """The section that goes on the page. Deterministic: same definition, same bytes."""
    open_tag, close_tag = _marker(form_name)
    fields = "\n".join(_field_html(form_name, field) for field in form["fields"])
    intro = (
        f'      <p class="section-intro">{html.escape(form["intro"])}</p>\n'
        if form.get("intro") else ""
    )
    return (
        f"{open_tag}\n"
        f'  <section class="section site-form-section" id="{html.escape(form_name)}-form">\n'
        '    <div class="container">\n'
        f'      <h2 class="section-title">{html.escape(form["title"])}</h2>\n'
        f"{intro}"
        f'      <form class="site-form" data-form="{html.escape(form_name)}" '
        f'data-success="{html.escape(form["success_message"])}" novalidate>\n'
        f"{fields}\n"
        # Off-screen rather than display:none -- a bot that renders the page at all still
        # sees a hidden field it is told not to fill, and fills it.
        '        <div class="form-trap" aria-hidden="true">\n'
        f'          <label for="f-{html.escape(form_name)}-{HONEYPOT_FIELD}">'
        "Leave this field empty</label>\n"
        f'          <input type="text" id="f-{html.escape(form_name)}-{HONEYPOT_FIELD}" '
        f'name="{HONEYPOT_FIELD}" tabindex="-1" autocomplete="off">\n'
        "        </div>\n"
        f'        <button class="btn btn-primary form-submit" type="submit">'
        f'{html.escape(form["submit_label"])}</button>\n'
        '        <p class="form-status" role="status" aria-live="polite"></p>\n'
        "      </form>\n"
        "    </div>\n"
        "  </section>\n"
        f"{close_tag}\n"
    )


def render_script(endpoint: str, form_key: str) -> str:
    """The one script that wires every form on the page.

    Deliberately plain: no library, no build step, nothing that can be missing when it
    runs. It fails loudly to the visitor rather than quietly to nobody -- a form that
    silently swallows a message is the failure this whole module exists to prevent, so the
    error path names the problem and points them at the phone number that is already on
    the page.
    """
    return f"""{SCRIPT_OPEN}
<script>
(function () {{
  var endpoint = {_js_string(endpoint)};
  var key = {_js_string(form_key)};
  var forms = document.querySelectorAll("form[data-form]");
  for (var i = 0; i < forms.length; i++) {{
    (function (form) {{
      var status = form.querySelector(".form-status");
      var button = form.querySelector(".form-submit");
      form.addEventListener("submit", function (event) {{
        event.preventDefault();
        if (form.getAttribute("data-sending") === "1") {{ return; }}
        var payload = {{}};
        var missing = null;
        var controls = form.querySelectorAll("[name]");
        for (var j = 0; j < controls.length; j++) {{
          var control = controls[j];
          var value = (control.value || "").trim();
          if (control.required && !value && !missing) {{ missing = control; }}
          payload[control.name] = value;
        }}
        if (missing) {{
          if (status) {{
            status.className = "form-status is-error";
            status.textContent = "Please fill in every field marked with a star.";
          }}
          missing.focus();
          return;
        }}
        var label = button ? button.textContent : "";
        form.setAttribute("data-sending", "1");
        if (button) {{ button.disabled = true; button.textContent = "Sending..."; }}
        if (status) {{ status.className = "form-status"; status.textContent = ""; }}
        fetch(endpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            form_key: key,
            form: form.getAttribute("data-form"),
            page: (location.pathname.split("/").pop() || "index.html"),
            payload: payload
          }})
        }}).then(function (response) {{
          if (!response.ok) {{ throw new Error("status " + response.status); }}
          form.reset();
          if (status) {{
            status.className = "form-status is-sent";
            status.textContent = form.getAttribute("data-success") ||
              "Thanks - your message has been sent.";
          }}
        }}).catch(function () {{
          if (status) {{
            status.className = "form-status is-error";
            status.textContent = "Sorry, that didn't send. Please try again, or " +
              "contact us using the details on this page.";
          }}
        }}).then(function () {{
          form.removeAttribute("data-sending");
          if (button) {{ button.disabled = false; button.textContent = label; }}
        }});
      }});
    }})(forms[i]);
  }}
}})();
</script>
{SCRIPT_CLOSE}
"""


def _js_string(value: str) -> str:
    """A JavaScript string literal that cannot escape its own quotes."""
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("<", "\\u003c")
        .replace("\n", "")
        .replace("\r", "")
    )
    return f'"{escaped}"'


# Kept here rather than in base.css on purpose. base.css is prepended at build time, so a
# form added to a site built last week would land against a stylesheet that has never
# heard of it; appending from one constant means both paths get the same rules from the
# same place, and there is no second copy to drift.
FORM_CSS = f"""
{CSS_MARKER}
.site-form {{ display: grid; gap: 1rem; max-width: 34rem; }}
.form-field {{ display: grid; gap: .35rem; }}
.form-label {{ font-weight: 600; font-size: .95rem; }}
.form-required {{ opacity: .6; }}
.form-input {{
  width: 100%;
  padding: .7rem .85rem;
  font: inherit;
  color: inherit;
  background-color: rgba(255, 255, 255, .9);
  border: 1px solid rgba(0, 0, 0, .25);
  border-radius: .4rem;
}}
.form-input:focus {{ outline: 2px solid currentColor; outline-offset: 1px; }}
.form-textarea {{ min-height: 8rem; resize: vertical; }}
.form-submit {{ justify-self: start; }}
.form-status {{ margin: 0; min-height: 1.4em; font-size: .95rem; }}
.form-status.is-sent {{ font-weight: 600; }}
.form-status.is-error {{ font-weight: 600; }}
/* Off screen, not hidden: a bot that renders the page still finds it and fills it in. */
.form-trap {{
  position: absolute;
  left: -10000px;
  width: 1px;
  height: 1px;
  overflow: hidden;
}}
{CSS_END_MARKER}
"""

# Bounded at both ends so the block can be taken out again as exactly as it went in.
# Without the closing marker, removing a form would leave thirty lines of rules for
# elements that no longer exist, on a live site, for ever.
_CSS_BLOCK_RE = re.compile(
    rf"\n*{re.escape(CSS_MARKER)}.*?{re.escape(CSS_END_MARKER)}\n?", re.DOTALL
)


def ensure_form_css(css: str) -> str:
    """The stylesheet with the form rules in it, added at most once."""
    if CSS_MARKER in css:
        return css
    return f"{css.rstrip()}\n{FORM_CSS}"


def strip_form_css(css: str) -> str:
    """The stylesheet as it was before any form was added to this site."""
    return _CSS_BLOCK_RE.sub("\n", css) if CSS_MARKER in css else css


# --------------------------------------------------------------- putting it on the page

# Where a form wants to be on a page that already has a closing call to action: above it,
# so the page still ends by asking for the sale rather than trailing off after a text box.
_CTA_BAND_RE = re.compile(r"[ \t]*<section\b[^>]*\bclass\s*=\s*[\"'][^\"']*\bcta-band\b",
                          re.IGNORECASE)
_MAIN_CLOSE_RE = re.compile(r"</main\s*>", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _insert_section(page: str, block: str) -> str:
    match = _CTA_BAND_RE.search(page)
    if match:
        return f"{page[:match.start()]}{block}{page[match.start():]}"
    matches = list(_MAIN_CLOSE_RE.finditer(page))
    if matches:
        at = matches[-1].start()
        return f"{page[:at]}{block}{page[at:]}"
    match = _BODY_CLOSE_RE.search(page)
    if match:
        return f"{page[:match.start()]}{block}{page[match.start():]}"
    # No <main> and no <body>: not a page any build here produces, but appending with no
    # separator keeps the strip-then-insert round trip byte-exact, which every later build
    # depends on.
    return f"{page}{block}"


def _insert_script(page: str, script: str) -> str:
    match = _BODY_CLOSE_RE.search(page)
    if match:
        return f"{page[:match.start()]}{script}{page[match.start():]}"
    return f"{page}{script}"


def apply_forms(
    files: dict[str, str], forms: dict | None, form_key: str | None, endpoint: str
) -> dict[str, str]:
    """The site's pages carrying exactly the forms it is defined to have.

    Returns a new dict; the input is never mutated. Safe to run on every build, which is
    the point: a form is defined once and re-applied for the life of the site, so it
    survives rebuilds that write every page from scratch, and it disappears from every
    page the moment its definition does.

    A form is skipped rather than half-built when the page it belongs to is not in `files`
    (a patch is handed one file at a time) or when there is no endpoint to post to -- a
    form that renders and posts nowhere is the one outcome worth failing to produce.
    """
    forms = forms or {}
    updated = dict(files)
    wanted = (
        {name: form for name, form in forms.items() if form.get("page") in updated}
        if (form_key and endpoint) else {}
    )

    # Every page loses every form block first, so a form that moved page, changed its
    # fields or was deleted leaves nothing behind. What is still defined goes back below.
    for filename, page in list(updated.items()):
        if not filename.endswith(".html"):
            continue
        updated[filename] = _SCRIPT_BLOCK_RE.sub("", _ANY_BLOCK_RE.sub("", page))

    for name, form in wanted.items():
        page_name = form["page"]
        updated[page_name] = _insert_section(updated[page_name], render_form(name, form))

    pages_with_forms = {form["page"] for form in wanted.values()}
    for page_name in pages_with_forms:
        updated[page_name] = _insert_script(
            updated[page_name], render_script(endpoint, form_key)
        )

    if STYLESHEET_FILE in updated:
        updated[STYLESHEET_FILE] = (
            ensure_form_css(updated[STYLESHEET_FILE]) if pages_with_forms
            else strip_form_css(updated[STYLESHEET_FILE])
        )
    return updated
