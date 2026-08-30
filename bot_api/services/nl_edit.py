import json

from bot_api.services.llm_client import (
    DailyLimitReached,
    LLMCallFailed,
    call_forced_tool,
)
from bot_api.services.edit_intent import plan_section
from bot_api.services.session import render_edit_context
from db.models import Business
from worker.codegen.builder import spec_from_business
from worker.codegen.outline import outline_site

PROMPT_TEMPLATE = """You manage edits to a small business's website via chat. The owner just sent you a message. Your job is to turn it into exactly one structured operation by calling one of the available functions.

Current site content:
{spec_json}
{site_outline}{lessons_section}{context_section}
Owner's message:
{raw_message}
{plan_section}

## Overlapping elements are never a spacing problem

If the owner says two sections, headings or blocks **overlap**, sit **on top of** each
other, or that one is **coming over** another, do NOT reach for set_style with margin or
padding. Space cannot separate elements that overlap: an element only overlaps its
neighbour when it has been taken out of the normal document flow (`position: absolute` or
`fixed`), and an element out of the flow ignores every margin you add around it. Adding
padding usually makes it worse by enlarging the thing that is covering the page.

Use patch_site on `style.css` and say to find the rule that positions that element and put
it back in the flow. A real owner reported the same overlap six times and got six spacing
changes, none of which could have worked.

## Content rules -- read carefully, these have different levels of freedom

Factual fields -- name, phone, email, address, hours, services and their prices, theme, category: NEVER invent these. If the owner's message doesn't supply the exact value, call clarify and ask for it.

Creative fields -- tagline, about: if the owner gives a vague or open instruction ("add more detail", "whatever you want", "make it sound nicer", "tell a story"), you SHOULD compose original marketing copy yourself, grounded in the real facts you already have (business name, category, existing services, existing tagline/about). Set drafted=true when you write text yourself rather than using the owner's exact words. You may use atmospheric, narrative language (scene-setting, tone, style) -- what you must never do is assert a specific verifiable fact that isn't already known (a founding year, an award, a statistic, a named person).

Attributed third-party claims -- customer quotes/reviews/testimonials, named awards, specific stats -- NEVER fabricate these under any function, even under the creative-fields allowance above. If the owner asks for a testimonials section (or similar) without giving you a real quote, call clarify and ask for the real quote, or offer to add a general "why customers choose us" section instead that doesn't pretend to quote anyone.

Infeasible requests -- only these are genuinely not possible: online booking or calendars, payments or checkout, live chat, embedded maps or social feeds, and customer logins. If asked for one of these, call clarify, say plainly and in ordinary words what it can't do yet, and suggest the closest thing it can do. Never silently attempt something broken.

## Enquiry forms

An enquiry form IS possible and fully supported -- "add a contact form", "let people message me from the site", "I want a booking enquiry form with their date and party size". Use `add_form` for all of these, never clarify and never patch_site.

The form is built and wired up for the owner automatically. Messages sent through it are stored and arrive in this chat the moment somebody sends one, and the owner can ask for them back at any time ("give me my site data").

- If they don't say which details to ask for, leave `fields` out: the form asks for name, email and message, which is what a contact form means to almost everyone.
- If they do say ("name, phone and what date they need"), list exactly those, in that order, using their own wording as each field's label. Never add fields they didn't ask for and never drop one they did.
- Forms go on the contact page unless the owner says otherwise. Only set `page` when they name a different one.
- **Never write a form yourself in a patch_site instruction.** A form written into the page by hand looks right and posts nowhere, so every message a customer sends is lost silently. `add_form` is the only way one is ever added.
- Use `remove_form` to take one off the site.

## Pricing sections

A pricing section is a fully supported thing to add, and no site is built with one, so "add a price section", "show your rates", "add a pricing table" is always a `patch_site`, never a clarify.

It is built as its own section on the page, using `pricing-grid` with one `pricing-card` per tier or service, each carrying a `pricing-name`, a `pricing-price` and optionally a `pricing-period` and a `pricing-features` list. Say that in the instruction so the section gets built properly.

**Never put prices in the FAQ.** That is what happened to a real owner who asked for a price section: with no pricing section defined anywhere, the model answered by adding price questions to the FAQ, which is not what anyone means by a price section.

Use the real `price_label` values from the site content above. If the business has services but no price labels, still add the section and write the tier names from the real services, leaving the price for the owner to fill in -- say so in your `summary` -- because an invented price on a live business site is the one thing you must never publish.

## Pictures the site already has

The site content above lists the owner's pictures under `logo_url` and `photo_urls`. Those are already uploaded and already on the site -- you can see them, so use them.

When the owner says "that picture", "the photo", "this image", "my photo" or similar, they mean a picture that is already there. Do NOT ask which one and do NOT ask them to send it again. Pick the one they most likely mean -- the most recently added photo, or the one the recent conversation above is about -- and write a `patch_site` instruction naming its full URL. Asking "which photo would you like to use?" when the site has photos is a real failure that happened to a real owner: they had already sent it, twice, and had to start over.

Only ask if the site genuinely has no pictures at all, in which case tell them they can send a photo straight to this chat.

Moving a picture is not the same as adding one. "Put the photo in the background", "move it lower", "make it smaller" all mean the SAME picture should end up in a new place -- your instruction must say to move or replace it, never just to add one, or the owner ends up with two copies of the same photo and has to ask you to delete one.

Everything else the owner is likely to ask for IS possible -- including FAQ panels that open when clicked, smooth scrolling, hover effects, extra sections, and switching between a one-page and a four-page site. See "What the site CAN do" below before ever telling someone no.

## Functions

- update_business_info: name/tagline/about/phone/email/address/theme/hours -- only include fields that changed. theme must be exactly one of: classic, modern, bold. Set drafted=true if you composed the tagline/about text yourself rather than using the owner's own words.
- add_service / update_service / remove_service: refer to an existing service by its current name exactly as shown above.
- set_style: THE DEFAULT for any change that is purely a style value on something already on the page -- taller, shorter, bigger, smaller, bolder, lighter, a different colour, more or less spacing, centred, rounded corners, a shadow. It edits the one value in place, so it is instant, exact, free, and cannot disturb anything else. Take `selector` and `from` straight from the map above; never invent a class name.
- patch_site: THE DEFAULT for anything about a specific visible thing on the existing site -- rename or reword a button/heading/label, move an element, remove an element, change where a link points, change a colour. The site is already live and will be edited surgically, so describe the change precisely and completely enough to act on without seeing the chat (e.g. "rename the 'Get started' button to 'Let's build' and point it at https://t.me/teko21bot"), and list the files it affects in `targets`.
- update_extra_instructions: ONLY for a durable, site-wide design preference the owner wants remembered and reapplied whenever the site is rebuilt from scratch (e.g. "always use a green navbar"). Do NOT use this for a one-off change to something already on the site -- that is patch_site. `mode` is "add" (default) or "clear".
- add_form: put a working enquiry form on the site, or change the one that is there. Messages sent through it are stored and land in the owner's chat straight away. Leave `fields` out for a plain name/email/message contact form.
- remove_form: take an enquiry form off the site.
- add_policies: add the four policy pages a payment provider checks for before it will let a business take money online -- terms and conditions, privacy policy, cancellation and refunds, and shipping and delivery. Use it for "add terms and conditions", "I need a privacy policy", "add the legal pages", "Razorpay is asking for policy pages", "my payment gateway rejected my website". The pages are written and linked automatically from the owner's own contact details, so never write them yourself in a patch_site instruction.
- remove_policies: take those policy pages back off the site.
- **These pages need the business's email, phone number and address**, and the bot will refuse to build them without all three. If your previous message asked the owner for a missing detail and THIS message is their answer, that is still `add_policies` -- put what they gave you into its `email`, `phone` and `address` and call it again. Never answer that with `update_business_info`: it saves the detail and forgets what it was for, so the owner has to ask for the pages a second time.
- change_layout: anything about HOW MANY PAGES the site has -- "keep only one page", "remove the other pages", "make this a landing page", "I want separate pages again". patch_site physically cannot add or delete pages, so never send those requests there.
- rebuild_site: the owner explicitly wants the whole site redone or redesigned from scratch ("recreate my website", "start over", "redesign the whole thing", "make it look completely different"). This throws away the current design, so only use it when they clearly mean the whole site, never for a single section.
- clarify: genuinely ambiguous, needs a real fact/quote you don't have, or an infeasible request -- ask a short, specific question or explain the limitation.
- not_an_edit: the message isn't a request to change the site at all (a greeting, thanks, or unrelated question).

## How to talk to the owner

The owner runs a business. They are not a developer, they have never seen their site's code, and they cannot answer a technical question about it. Everything you say to them appears in a chat message, so write it the way a helpful shop assistant would speak.

**Never use these words with the owner**: HTML, CSS, JavaScript, markup, element, attribute, tag, class, ID, selector, static, div, anchor, onclick, code, file, index.html or any filename. Say "the page", "the menu", "the FAQ section", "the button", "the colour" instead.

**Never ask the owner to describe or paste their code**, or to tell you what is currently on the page. You can already see their site. If you need to know something about it, look at the site content given above -- do not ask them.

A real failure to avoid: an owner said their FAQ items would not open, and got back *"The site is static HTML/CSS and cannot add interactive behavior without JavaScript or CSS-targeting... could you tell me the current markup of the FAQ section?"* That is three mistakes at once -- jargon, a request for code they don't have, and a claim that was simply wrong.

## What the site CAN do

Do not tell the owner something is impossible before checking this list. These all work with no JavaScript, and you should just do them:

- **FAQ or panels that open and close when clicked** -- fully supported. If an owner says their FAQ does not open, that is a fixable bug, not a limitation: patch the FAQ so each item is a `<details class="faq-item">` with `<summary class="faq-question">` and `<div class="faq-answer">`.
- Smooth scrolling to a section, hover effects, animations, transitions, swipeable card rows -- all supported.
- Any wording, colour, spacing, layout, image or section change -- supported.

- **A working enquiry form** -- fully supported, and it really sends: use `add_form`.
- **The policy pages a payment provider demands** (terms, privacy, refunds, shipping) -- fully supported: use `add_policies`. Getting rejected by Razorpay or Cashfree for missing these is common and this fixes it, so never tell an owner it cannot be done.

Genuinely not possible today, and worth saying plainly (in ordinary words, without naming technologies): online booking or payments, live chat, embedded maps, and customer logins. For those, say what it can't do yet and suggest showing their phone number, email or enquiry form instead.

## Never silently drop part of a request

If the owner asks for several things in one message, your single operation must cover ALL of them. Real failure: "Remove navbar section and instead of red bg, make it light gray" was turned into an instruction about the colour only, so the navbar stayed and the owner paid for an edit that did half of what they asked.

If you cannot cover everything in one operation, call clarify instead: say plainly which part you can do and which you cannot, and ask them to confirm. Never guess, and never quietly do the easier half. If you are unsure what an instruction refers to -- which element, which section, which page -- ask rather than picking something and hoping.

## set_style or patch_site?

Use **set_style** when the thing already exists on the page and only a value about how it
looks is changing. Height, width, size, weight, colour, background colour, spacing,
padding, alignment, corner radius, shadow: all set_style. This is most of what owners
ask for, and it is applied without a model rewriting the file, so it cannot go wrong in
the ways the old route did.

Use **patch_site** when the page's content or structure changes: wording, a new or
removed element, a link pointing somewhere else, a picture moving, an FAQ that needs
rebuilding.

If a request needs both -- "make the button say Book now and make it green" -- use
patch_site and describe both parts. Never split one request across two operations, and
never do the easier half.

## Before anything else: is it already true?

The map above now includes the styling values actually in force on the live site right
now. Read them before you decide anything.

If the site already has what the owner is describing, do NOT send the same instruction
again. Applying a change that is already applied changes nothing, the build is rejected,
and the owner is told "I couldn't work out how to make that change" -- which reads as a
flat refusal of something they can plainly see needs doing.

A real failure, and the reason this section exists: an owner asked six times across two
days to make the top section taller and its heading bold. After the very first attempt
the section was already 800px tall and the heading was already bold. Every later attempt
re-sent "set .hero min-height to 800px and .hero-title font-weight to bold", changed
nothing, failed, and told them it could not be done. Six charges, two days, no progress,
and by the end they were typing raw code into the chat out of frustration.

When what they are asking for is already in force, choose one -- never repeat it:

- **They are asking again, or say it did not work.** Then they want MORE of it. Send a
  patch_site instruction with a value clearly beyond the current one, and name both
  values in the instruction so it cannot be mistaken for the same change again -- e.g.
  "the hero is already min-height 800px and that is not enough for the owner: make it
  1200px". Add a short note in the same instruction about why it may have looked
  unchanged, if you can see the reason.
- **It is the first time they have asked and it is already that way.** Call clarify. Tell
  them plainly, in ordinary words and without naming any code, that it already looks that
  way -- "your top section is already about twice the height of a normal one and the
  heading is already bold" -- and ask what they would like instead. Never quietly re-send
  it.

## What "bigger", "taller" and "bolder" have to mean

Never send a value that is smaller than the current one when the owner asked for more.

A real failure: the heading was set to a size that renders up to 48px on a laptop, and
"increase the font size" was turned into a flat 32px -- a third SMALLER. The owner
replied "font size not increased", and the next attempt enlarged the paragraph underneath
instead, so that site now has a sub-heading bigger than the heading above it.

- Read the current value out of the map and pick one clearly beyond it, same direction.
- Where the map says a value "renders between 28px and 48px", you must beat the TOP of
  that range, not the bottom.
- Headings are already bold. Asking for bold on a heading changes nothing a visitor can
  see -- if they want it heavier say 800 or 900, and if they only want it to stand out
  more, say so and offer a size or colour change instead.
- Name the one element you mean. Enlarging the sub-heading when the owner meant the
  heading is worse than doing nothing, because it leaves the page looking wrong.

## Writing a patch_site instruction

The map above is what is really on the site. **Use it, and never invent anything that isn't in it** -- not a class name, not a section, not a picture. A name you guess will match nothing, and the change then silently does not happen.

A real failure: an owner asked to make the top section taller and its heading bold. The instruction said `change .hero-section min-height to 100vh`. There is no `.hero-section` -- the map shows that section is `.hero` -- so the file came back untouched and the owner was told twice their change could not be made. They had explained it clearly both times.

Write the instruction the way you would describe it to someone standing in front of the page:
- name the part by **what a visitor sees**, and match it against the map -- "the section at the top of the home page (`section.hero`)", "the heading that reads 'Software that works for you'", "the cards under 'Why clients choose me'";
- say **what should change**, including any exact value the owner gave ("twice as tall", "bold", "dark green", "links to https://...");
- before asking the owner anything, check the map -- if it already answers your question, don't ask it.

Whoever applies your instruction has the whole file in front of them. Your job is to say clearly and correctly what the owner wants changed and where it is.

## Choosing targets for patch_site

{files_section}

Never list a file that does not exist, and never list a file the change doesn't touch: files you leave out are kept exactly as they are, which is what protects the rest of the owner's site.
"""

UPDATE_BUSINESS_INFO_TOOL = {
    "name": "update_business_info",
    "description": "Update one or more simple site fields. Only include fields the owner actually mentioned.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "tagline": {"type": "string"},
            "about": {"type": "string", "description": "single continuous line, no embedded newline characters"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "address": {"type": "string"},
            "theme": {"type": "string", "description": "classic, modern, or bold"},
            "hours": {"type": "string", "description": "free-text hours, e.g. 'Mon-Fri 9-6'"},
            "drafted": {
                "type": "boolean",
                "description": "true if you personally composed the tagline/about text rather than using the owner's exact words",
            },
        },
    },
}

UPDATE_EXTRA_INSTRUCTIONS_TOOL = {
    "name": "update_extra_instructions",
    "description": "Record a durable, site-wide design preference to reapply on every future rebuild (e.g. 'always use a green navbar'). NOT for changing something already on the site -- use patch_site for that.",
    "parameters": {
        "type": "object",
        "properties": {
            "instructions": {"type": "string", "description": "single continuous line, no embedded newline characters"},
            # 'replace' deliberately removed: the model reached for it constantly and each
            # use silently destroyed the owner's earlier preferences (four in a row were
            # lost in real testing). Only additive changes and an explicit wipe remain.
            "mode": {
                "type": "string",
                "description": "'add' (default -- append, keeping existing preferences) or "
                "'clear' (wipe all preferences; doesn't need `instructions`).",
            },
        },
    },
}

SET_STYLE_TOOL = {
    "name": "set_style",
    "description": (
        "Change one or more style values on something already on the page: size, weight, "
        "colour, spacing, height, width, alignment, corner radius, shadow. Applied exactly "
        "and instantly by editing the one line that holds the value -- no rewriting, no "
        "risk to the rest of the site, and it costs the owner nothing. ALWAYS prefer this "
        "over patch_site when the change is purely a style value on an existing element."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "One entry per value being changed; cover everything the owner asked for.",
                "items": {
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "A plain class selector taken from the map above, e.g. .hero "
                            "or .hero-title. Never a guess, never a compound like .hero .title.",
                        },
                        "property": {
                            "type": "string",
                            "description": "The style property, e.g. min-height, font-size, font-weight, "
                            "color, background-color, padding, text-align.",
                        },
                        "from": {
                            "type": "string",
                            "description": "The value the map says is in force right now, if any. Used to "
                            "confirm the change is a real one rather than the value it already has.",
                        },
                        "value": {
                            "type": "string",
                            "description": "The new value, e.g. 1200px. Where the owner asked for more of "
                            "something, this must be clearly beyond `from`, not equal to it.",
                        },
                    },
                    "required": ["selector", "property", "value"],
                },
            },
            "summary": {
                "type": "string",
                "description": "What this does in the owner's everyday words, with no code, no class "
                "names and no property names in it -- e.g. 'make the top section taller and the "
                "heading heavier'. It is shown to them as-is.",
            },
        },
        "required": ["changes", "summary"],
    },
}

PATCH_SITE_TOOL = {
    "name": "patch_site",
    "description": "Make one specific, surgical change to the existing live site: rename/reword an element, move it, remove it, repoint a link, or change a colour. Files not listed in `targets` are left byte-identical.",
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The change to make, precise and self-contained -- it is applied "
                "without the chat history, so name the element and the exact new value. "
                "Single continuous line, no embedded newline characters.",
            },
            "targets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which files this change touches: any of index.html, about.html, "
                "services.html, contact.html, style.css. Header/nav/logo/footer changes touch all "
                "four HTML files; pure colour/font/spacing changes touch style.css only. "
                "Anything involving a PICTURE -- moving a photo, putting one behind text as a "
                "background, resizing or removing one -- must include the page the picture is on "
                "as well as style.css, because the picture itself sits on the page, not in the "
                "stylesheet. Listing style.css alone for a photo change does nothing at all.",
            },
        },
        "required": ["instruction", "targets"],
    },
}

CHANGE_LAYOUT_TOOL = {
    "name": "change_layout",
    "description": "Switch between a one-page landing site and a four-page site. Use this for anything about how many pages the site has -- 'keep only one page', 'make it a landing page', 'I want separate pages'. Patching cannot add or remove pages, so this is the only way.",
    "parameters": {
        "type": "object",
        "properties": {
            "layout": {
                "type": "string",
                "description": "'landing' for one scrolling page whose menu jumps to sections, "
                "or 'multipage' for four separate linked pages.",
            },
        },
        "required": ["layout"],
    },
}

REBUILD_SITE_TOOL = {
    "name": "rebuild_site",
    "description": "Regenerate the entire site from scratch with a fresh design. Only when the owner clearly wants the whole site redone, not a single section.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short restatement of what the owner wants from the rebuild.",
            },
        },
    },
}

ADD_SERVICE_TOOL = {
    "name": "add_service",
    "description": "Add a new service or product to the site.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price_label": {"type": "string"}},
        "required": ["name"],
    },
}

UPDATE_SERVICE_TOOL = {
    "name": "update_service",
    "description": "Change an existing service's name and/or price.",
    "parameters": {
        "type": "object",
        "properties": {
            "current_name": {"type": "string", "description": "the service's current name"},
            "new_name": {"type": "string"},
            "new_price_label": {"type": "string"},
        },
        "required": ["current_name"],
    },
}

REMOVE_SERVICE_TOOL = {
    "name": "remove_service",
    "description": "Remove an existing service from the site.",
    "parameters": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "the service's current name"}},
        "required": ["name"],
    },
}

ADD_FORM_TOOL = {
    "name": "add_form",
    "description": (
        "Put a working enquiry form on the site, or replace the one already there. The form "
        "is built and wired up automatically -- messages sent through it are stored and reach "
        "the owner's chat immediately. Use this for any request to let visitors send a "
        "message, an enquiry or a booking request from the site. NEVER write a form by hand "
        "in a patch_site instruction: it would look right and send nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "description": "The details to ask the visitor for, in order. LEAVE THIS OUT "
                "entirely unless the owner named the details they want -- omitted, the form "
                "asks for name, email and message.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {
                            "type": "string",
                            "description": "What the visitor sees above the box, in the "
                            "owner's own wording, e.g. 'Your name', 'Which date', 'Party size'.",
                        },
                        "type": {
                            "type": "string",
                            "description": "One of text, email, tel, textarea, number, date. "
                            "Leave out and it is worked out from the label.",
                        },
                        "required": {
                            "type": "boolean",
                            "description": "true only if the owner said this one must be "
                            "filled in. A required field the visitor cannot supply is a form "
                            "they abandon.",
                        },
                    },
                    "required": ["label"],
                },
            },
            "page": {
                "type": "string",
                "description": "Only when the owner names a page other than contact: "
                "'home', 'about', 'services' or 'contact'.",
            },
            "title": {
                "type": "string",
                "description": "The heading above the form, e.g. 'Book a table' or 'Send us "
                "a message'. Write one that suits this business.",
            },
            "submit_label": {
                "type": "string",
                "description": "What the button says, e.g. 'Send enquiry', 'Request a quote'.",
            },
            "success_message": {
                "type": "string",
                "description": "What the visitor sees after sending, e.g. 'Thanks - we'll "
                "come back to you within a day.'",
            },
            "form": {
                "type": "string",
                "description": "A short name for this form, lowercase, e.g. 'contact' or "
                "'booking'. Defaults to 'contact'. Use the SAME name to change an existing "
                "form, and a new one only for a genuinely second form on the site.",
            },
        },
    },
}

REMOVE_FORM_TOOL = {
    "name": "remove_form",
    "description": "Take an enquiry form off the site. Enquiries already received are kept.",
    "parameters": {
        "type": "object",
        "properties": {
            "form": {
                "type": "string",
                "description": "The form's name, e.g. 'contact'. Leave out if the site has "
                "only one.",
            },
        },
    },
}

CLARIFY_TOOL = {
    "name": "clarify",
    "description": "The request is ambiguous or not supported by the other functions -- ask the owner a question instead of guessing.",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
}

NOT_AN_EDIT_TOOL = {
    "name": "not_an_edit",
    "description": "The message is not a request to change the site (e.g. a greeting or thanks).",
    "parameters": {"type": "object", "properties": {}},
}

ADD_POLICIES_TOOL = {
    "name": "add_policies",
    "description": (
        "Add the four policy pages a payment provider requires before it will let a "
        "business take money online: terms and conditions, privacy policy, cancellation "
        "and refunds, and shipping and delivery. Use this for any request about policy "
        "pages, legal pages, terms, privacy, refunds, or being approved by a payment "
        "provider such as Razorpay, Cashfree, PayU or Stripe. The pages are written and "
        "linked automatically from the business's own contact details -- NEVER write them "
        "by hand in a patch_site instruction, because a policy page is a commitment and "
        "invented wording becomes a promise the owner never made."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "refund_days": {
                "type": "integer",
                "description": "How many days a customer has to ask for a full refund. "
                "ONLY when the owner said a number; left out, it is 7.",
            },
            "legal_name": {
                "type": "string",
                "description": "The registered business name, ONLY if the owner gave one "
                "that differs from the name on the site -- e.g. 'Sharma Traders Pvt Ltd' "
                "for a site called 'Sharma Sweets'. A payment provider compares the two.",
            },
            "email": {
                "type": "string",
                "description": "The business's email address, if this message contains it.",
            },
            "phone": {
                "type": "string",
                "description": "The business's phone number, if this message contains it.",
            },
            "address": {
                "type": "string",
                "description": "The business's full postal address, if this message "
                "contains it.",
            },
        },
    },
}

REMOVE_POLICIES_TOOL = {
    "name": "remove_policies",
    "description": "Take the terms, privacy, refund and shipping pages back off the site.",
    "parameters": {"type": "object", "properties": {}},
}

TOOLS = [
    UPDATE_BUSINESS_INFO_TOOL,
    ADD_SERVICE_TOOL,
    UPDATE_SERVICE_TOOL,
    REMOVE_SERVICE_TOOL,
    UPDATE_EXTRA_INSTRUCTIONS_TOOL,
    SET_STYLE_TOOL,
    PATCH_SITE_TOOL,
    ADD_FORM_TOOL,
    REMOVE_FORM_TOOL,
    ADD_POLICIES_TOOL,
    REMOVE_POLICIES_TOOL,
    CHANGE_LAYOUT_TOOL,
    REBUILD_SITE_TOOL,
    CLARIFY_TOOL,
    NOT_AN_EDIT_TOOL,
]


class EditParseFailed(Exception):
    pass


MULTIPAGE_FILES_SECTION = """This site has exactly these files: index.html (home: hero, intro, highlights, offerings preview, why-choose-us, closing call to action), about.html, services.html (offerings, process steps, FAQ), contact.html (contact details, hours), style.css (all colours, fonts, spacing, layout).

The header, nav, logo and footer appear on all four pages -- a change to any of those must list all four HTML files. A colour, font or spacing change targets style.css only. Anything else targets just the page it appears on."""

LANDING_FILES_SECTION = """This site is a ONE-PAGE landing site. It has exactly two files and no others:

- index.html -- the entire site on one page: the hero, the about section (id="about"), the services section (id="services"), a process section, an FAQ, and the contact section (id="contact"), plus the header and footer.
- style.css -- all colours, fonts, spacing and layout.

There is NO about.html, services.html or contact.html -- those are sections inside index.html. A request about the about/services/contact content targets **index.html**. A colour, font or spacing change targets **style.css** only."""


def _files_section(business: Business) -> str:
    return LANDING_FILES_SECTION if business.layout == "landing" else MULTIPAGE_FILES_SECTION


async def parse_edit_message(
    raw_message: str,
    business: Business,
    context: list[dict] | None = None,
    files: dict[str, str] | None = None,
    plan: dict | None = None,
    lessons: str = "",
) -> tuple[dict, dict]:
    """Parse a free-text edit message into a structured operation.

    Returns ({"operation": <name>, **args}, usage). A successful "clarify" or
    "not_an_edit" call is a valid result, not a failure -- EditParseFailed is only raised
    on a technical failure (API errors, exhausted retries, malformed response).

    The usage is returned rather than discarded so the caller can bill it: every message
    the owner sends costs tokens even when it turns out not to be an edit at all, and
    dropping that made the quota under-report real spend.

    `lessons` is what has already worked on this site, rendered by
    worker/learning/lessons.py. Passed in rather than fetched here so this stays free of
    the database -- the eval harness calls it with no session at all, and must keep being
    able to.
    """
    spec = spec_from_business(business)
    # Shown to the parser and to nobody else. The same spec feeds the site builder, which
    # must never see a form definition: it is banned from writing one, and handing it the
    # shape of the thing it may not write is an invitation to try.
    if getattr(business, "forms", None):
        spec["enquiry_forms"] = {
            name: {
                "page": form.get("page"),
                "asks_for": [field["label"] for field in form.get("fields") or []],
            }
            for name, form in business.forms.items()
        }
    spec_json = json.dumps(spec, indent=2, ensure_ascii=False)
    outline = outline_site(files)
    prompt = PROMPT_TEMPLATE.format(
        spec_json=spec_json,
        site_outline=(
            f"\nWhat is actually on the site right now (the map):\n{outline}\n" if outline else ""
        ),
        context_section=render_edit_context(context),
        raw_message=raw_message,
        plan_section=plan_section(plan),
        files_section=_files_section(business),
        lessons_section=lessons,
    )

    try:
        op, usage = await call_forced_tool(prompt, TOOLS)
    except DailyLimitReached:
        # Subclasses LLMCallFailed, so without this it would be wrapped into a
        # generic parse failure and reported as "try again in a moment" -- advice that
        # cannot work, because the cap resets on the day, not in a moment.
        raise
    except LLMCallFailed as exc:
        raise EditParseFailed(f"Edit parsing failed: {exc}") from exc
    return op, usage
