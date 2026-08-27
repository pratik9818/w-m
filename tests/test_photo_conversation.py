"""Four complaints from one owner, all about the bot not holding a thread.

    "my quota was exhausting, after that I sent 'now use this' or 'please update the
     photo' which he recommended, but he forgot and said 'can you give again these
     options or please clarify' -- so it means bot do not remember anything about past
     messages"

The recommendation had been made by the *worker* -- a different process, minutes later --
and nothing the worker said had ever been written to the conversation buffer. The bot could
not remember a single thing it had told an owner from that side: not a result, not a
failure, not a suggestion. Everything here is about closing that, and the smaller versions
of the same hole around photographs.
"""

import inspect

import pytest

from bot_api.bot.filters import is_affirmation, is_declining
from bot_api.bot.handlers import photos as photo_handler
from bot_api.services.edit_ops import picture_source_answer, wants_a_picture
from bot_api.services.session import _EDIT_CONTEXT_MAX_TURNS, render_edit_context
from worker.tasks import notify


# ------------------------------------------------- what the bot can remember saying

class _FakeBot:
    """Records what was sent instead of talking to Telegram."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append(caption)


class _FakeBusiness:
    id = "11111111-1111-1111-1111-111111111111"
    name = "Rise & Crumb"
    owner_telegram_id = 42
    deployment_url = "https://rise-and-crumb.example"


@pytest.fixture
def recorded(monkeypatch):
    """Capture the turns the worker writes, without a Redis."""
    turns = []

    async def fake_push(redis, business_id, raw_message, outcome):
        turns.append((str(business_id), raw_message, outcome))

    monkeypatch.setattr(notify, "push_edit_turn", fake_push)
    monkeypatch.setattr(notify, "get_redis", lambda: None)
    return turns


@pytest.mark.asyncio
async def test_the_worker_records_a_failure_it_reported(recorded):
    """The exact complaint. The allowance message is sent from the worker, and until it was
    recorded here, "ok use the one you suggested" was read against nothing at all."""
    bot = _FakeBot()

    await notify.notify_owner_failure(bot, _FakeBusiness(), "quota")

    assert bot.sent, "the owner was told nothing"
    assert recorded, "the worker spoke to the owner and left no trace in the conversation"
    assert "allowance" in recorded[0][2]["bot_said"]


@pytest.mark.asyncio
async def test_the_worker_records_a_result_it_reported(recorded):
    bot = _FakeBot()

    await notify.notify_owner_success(bot, _FakeBusiness())

    assert recorded, "'your site is live' left no trace in the conversation"
    assert "is live" in recorded[0][2]["bot_said"]


@pytest.mark.asyncio
async def test_a_broken_memory_does_not_break_the_message(monkeypatch):
    """This runs at the end of a pipeline that has already succeeded. A Redis that is down
    should cost the bot its memory of one message, not the owner their result."""
    async def boom(*a, **k):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(notify, "push_edit_turn", boom)
    monkeypatch.setattr(notify, "get_redis", lambda: None)
    bot = _FakeBot()

    await notify.notify_owner_success(bot, _FakeBusiness())

    assert bot.sent, "the owner must still be told their site is live"


def test_progress_chatter_is_not_recorded():
    """"Writing your site..." is not something an owner replies to, and three of them per
    build would push the real exchange out of a buffer that only holds so much."""
    assert "_remember" not in inspect.getsource(notify.notify_owner_progress)


def test_what_the_bot_said_is_rendered_for_the_next_message():
    rendered = render_edit_context([
        {"raw_message": "(waiting for their site)",
         "outcome": {"bot_said": "You've used up your allowance for now, so I couldn't "
                                 "build Rise & Crumb."}},
    ])
    assert "You told them" in rendered
    assert "used up your allowance" in rendered


def test_a_photograph_is_remembered_by_its_url():
    """"Put that picture in the background" needs the picture to still be findable."""
    rendered = render_edit_context([
        {"raw_message": "(sent a photo)",
         "outcome": {"photo_url": "https://cdn.example/bread.jpg",
                     "bot_asked": "Whereabouts would you like it?"}},
    ])
    assert "https://cdn.example/bread.jpg" in rendered
    assert "Do not ask them to send it again" in rendered
    # Both halves survive: the picture and the question asked about it.
    assert "Whereabouts would you like it?" in rendered


def test_the_buffer_holds_a_real_conversation():
    """The owner asked for "at least last 5 to 10 messages". Each exchange is a turn, and
    the worker now contributes turns of its own, so the buffer has to be bigger than the
    number of owner messages it is meant to cover."""
    assert _EDIT_CONTEXT_MAX_TURNS >= 10


def test_a_full_buffer_keeps_the_most_recent_turns():
    turns = [
        {"raw_message": f"message {i}", "outcome": {"bot_said": f"reply {i}"}}
        for i in range(1, _EDIT_CONTEXT_MAX_TURNS + 1)
    ]
    rendered = render_edit_context(turns)
    assert f"message {_EDIT_CONTEXT_MAX_TURNS}" in rendered
    assert "message 1" in rendered


# ------------------------------------------------- where a picture goes, in words

def test_the_upload_reply_no_longer_hands_out_a_menu():
    """Five buttons are the bot listing the five places it can imagine. An owner who wanted
    the picture somewhere else had to pick the closest wrong one."""
    source = inspect.getsource(photo_handler)
    assert "photo_placement_keyboard" not in source
    assert "reply_markup" not in source


def test_the_question_invites_words_and_says_anything_goes():
    assert "your own words" in photo_handler.PLACEMENT_QUESTION
    assert "somewhere else entirely" in photo_handler.PLACEMENT_QUESTION


@pytest.mark.parametrize("answer, expected", [
    ("as my logo", "logo"),
    ("use this as the logo please", "logo"),
    ("at the top", "hero"),
    ("big picture at the top", "hero"),
    ("make it the banner", "hero"),
    ("behind the text at the top", "background"),
    ("as a background", "background"),
    ("put it in the gallery", "gallery"),
    ("next to my about text", "about"),
])
def test_ordinary_answers_are_read_without_a_model_call(answer, expected):
    assert photo_handler.placement_from_text(answer) == expected


def test_behind_the_text_at_the_top_is_a_background_not_a_hero():
    """The specific reading wins over the general one. "Behind the text at the top"
    contains "top", and an owner who asked for this twice got the picture put above the
    text both times."""
    assert photo_handler.placement_from_text("behind the text at the top") == "background"


@pytest.mark.parametrize("answer", [
    "under the opening hours",
    "beside the second service",
    "somewhere near the prices",
])
def test_an_answer_this_module_cannot_place_is_not_an_error(answer):
    """None means "more specific than my five slots", and that goes to the edit pipeline,
    which writes an instruction instead of picking from a list."""
    assert photo_handler.placement_from_text(answer) is None


def test_an_answer_naming_an_unknown_spot_still_places_the_picture():
    """The exact bug, from a live exchange.

        Owner: (sends a photo)
        Bot:   "Whereabouts on your site would you like it?"
        Owner: "Put this image in 2 section and remove current one"
        Bot:   "Send me your own picture, or I can find one for you."

    "2 section" is not one of the five spots, so the handler passed the message to the edit
    pipeline -- which knew nothing about the picture being held here and offered to find
    one, seconds after the owner sent one. The answer to "where would you like it?" belongs
    to whoever asked the question, so it is placed here using their own words.
    """
    source = inspect.getsource(photo_handler.on_placement_reply)
    assert "SkipHandler" not in source, (
        "handing this to the editor is what caused the bot to ask for a photo it was holding"
    )
    assert "_place_photo(message, message.from_user.id, words=message.text)" in source


def test_the_free_form_instruction_pins_the_url_and_forbids_inventing_one():
    """The owner's words are the instruction, so everything that stops a patch going wrong
    has to be said around them."""
    instruction = photo_handler.FREEFORM_PLACEMENT
    assert "{words}" in instruction and "{url}" in instruction
    assert "character for character" in instruction
    assert "never invent another one" in instruction
    # Their real message asked for the current picture to go. Both left behind is the
    # failure this line exists to prevent.
    assert "rather than leaving both" in instruction


def test_counting_sections_is_explained_rather_than_assumed():
    """"2 section" only means something if the model counts the way an owner does."""
    assert "not counting the header or the footer" in photo_handler.FREEFORM_PLACEMENT


@pytest.mark.parametrize("words, expected", [
    ("put it in the 2nd section", "index.html"),
    ("on the contact page please", "contact.html"),
    ("in the services section", "services.html"),
    ("under the opening hours", "index.html"),
])
def test_a_free_form_placement_edits_the_page_they_named(words, expected):
    """A section is almost always on the home page, which is why that is the fallback --
    but "on the contact page" says otherwise, and editing index.html instead would change
    the wrong file and look like nothing happened."""
    pages = ["index.html", "about.html", "services.html", "contact.html"]
    assert photo_handler._page_named_in(words, pages) == [expected]


def test_a_landing_site_has_only_one_page_to_aim_at():
    assert photo_handler._page_named_in("on the contact page", ["index.html"]) == ["index.html"]


def test_the_pending_photo_check_is_a_filter_not_a_body_check():
    """Same reason: deciding inside the handler is deciding too late."""
    assert "_has_pending_photo" in inspect.getsource(photo_handler.on_placement_reply.__module__
                                                     and photo_handler)
    decorated = inspect.getsource(photo_handler)
    assert "@router.message(default_state, has_text, _has_pending_photo)" in decorated


def test_the_bot_never_offers_to_find_a_picture_while_holding_one():
    """The second half of the same bug. Even once the placement is handled properly, an
    owner who sent a photo, got distracted, and came back to it must not be offered a
    stock photograph -- theirs is right there."""
    from bot_api.bot.handlers import edit as edit_handler

    sent_a_photo = [
        {"raw_message": "(sent a photo)",
         "outcome": {"photo_url": "https://cdn.example/shopfront.jpg",
                     "bot_asked": "Whereabouts would you like it?"}},
        {"raw_message": "actually make the heading bigger first",
         "outcome": {"applied": "set_style", "summary": "made the heading bigger"}},
    ]

    assert edit_handler._photo_already_in_hand(sent_a_photo)
    assert not edit_handler._photo_already_in_hand([
        {"raw_message": "make the heading bigger",
         "outcome": {"applied": "set_style", "summary": "done"}},
    ])
    assert not edit_handler._photo_already_in_hand([])
    assert not edit_handler._photo_already_in_hand(None)


def test_the_picture_offer_is_conditional_on_not_having_one():
    from bot_api.bot.handlers import edit as edit_handler

    source = inspect.getsource(edit_handler.catch_all_edit)
    assert "wants_a_picture(raw_message) and not _photo_already_in_hand(context)" in source


@pytest.mark.parametrize("text", ["no", "never mind", "cancel", "No thanks.", "forget it"])
def test_backing_out_is_understood(text):
    assert is_declining(text)


@pytest.mark.parametrize("text", ["no, put it at the top", "not the logo, the banner"])
def test_a_no_carrying_an_instruction_is_not_a_cancellation(text):
    """Reading these as "cancel" would throw away the picture *and* the instruction that
    came with it."""
    assert not is_declining(text)
    assert not is_affirmation(text)


# ------------------------------------------------- telling owners both options exist

@pytest.mark.parametrize("request_text", [
    "add an image to my home page",
    "there are no images on my site",
    "can you put a picture of bread on there",
    "the site needs photos",
    "change the logo",
])
def test_asking_for_a_picture_is_recognised(request_text):
    assert wants_a_picture(request_text)


@pytest.mark.parametrize("request_text", [
    "make the image smaller",
    "remove the photo at the top",
    "move the picture down",
    "the photo is too big",
])
def test_working_on_a_picture_already_there_is_not(request_text):
    """These are about a photograph the site already has. Offering to go and find one
    would be answering a question nobody asked."""
    assert not wants_a_picture(request_text)


def test_both_ways_of_getting_a_picture_are_named():
    """The whole point. An owner wrote "there are no images whole website is empty" and
    then waited, because nothing had told them either option existed."""
    question = photo_handler and __import__(
        "bot_api.bot.handlers.edit", fromlist=["PICTURE_QUESTION"]).PICTURE_QUESTION
    assert "your own picture" in question.lower() or "send me your own" in question.lower()
    assert "find one for you" in question.lower()
    assert "attach" in question.lower()


@pytest.mark.parametrize("answer, expected", [
    ("i will send one", "own"),
    ("ill upload it", "own"),
    ("i have a photo", "own"),
    ("hold on", "own"),
    ("you find one", "find"),
    ("you pick something", "find"),
    ("i dont have any pictures", "find"),
    ("anything is fine", "find"),
    ("up to you", "find"),
])
def test_which_way_they_chose_is_read_without_a_model_call(answer, expected):
    assert picture_source_answer(answer) == expected


def test_saying_both_things_resolves_to_the_half_that_decides():
    """"I don't have one, you pick" says both. What happens next is the second half."""
    assert picture_source_answer("i don't have one, you pick") == "find"


def test_an_unrelated_reply_is_neither():
    assert picture_source_answer("make the heading bigger") is None


def test_finding_a_photograph_costs_no_model_call():
    """`find_photos` pays a model to plan a whole shoot. For "add a picture to my home
    page" the answer is one photograph, and the business already says what it should be
    of."""
    from worker.codegen import photos as photo_module
    source = inspect.getsource(photo_module.find_one_photo)
    assert "call_forced_tool" not in source
    assert "_plan_shots" not in source


@pytest.mark.parametrize("text, expected_absent", [
    ("can you add a nice image to my page", "image"),
    ("please add some photos", "photos"),
])
def test_the_search_query_describes_the_picture_not_the_request(text, expected_absent):
    """Left in, these are what the photo library actually searches for -- "add a nice image
    to my page" returns pictures of pages."""
    from worker.codegen.photos import _SEARCH_STOPWORDS_RE
    cleaned = " ".join(_SEARCH_STOPWORDS_RE.sub(" ", f"{text} Bakery").split())
    assert expected_absent not in cleaned.lower()
    assert "Bakery" in cleaned
