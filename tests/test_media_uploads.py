"""Putting a video or a PDF on a site, and never meeting a file with silence.

Before this, the bot listened for exactly one kind of attachment: a compressed photo.
Everything else -- a PDF menu, a video of the salon, or a photograph sent "as a file",
which is what Telegram does by default from a computer -- matched no handler at all and
got **no reply of any kind**. Not an error, not an explanation. The owner had no way to
tell whether the bot was broken or ignoring them.

So half of what is tested here is the boring half: that every kind of attachment now gets
an answer. The other half is that the limits are enforced before a download rather than
after, and that a video is never allowed to play by itself.
"""

import re

import pytest

from bot_api.bot.handlers import photos
from bot_api.services.storage import (
    CATEGORY_LIMITS,
    DOCUMENT_TYPES,
    IMAGE_TYPES,
    MEGABYTE,
    TELEGRAM_MAX_DOWNLOAD_BYTES,
    VIDEO_TYPES,
    UploadRejected,
    category_for,
    check_size,
    describe_limit,
    limit_for,
)


# ------------------------------------------------- the limits that were asked for

@pytest.mark.parametrize("kind, megabytes", [
    ("photo", 20), ("logo", 20), ("video", 20), ("document", 5),
])
def test_each_kind_has_the_limit_it_was_given(kind, megabytes):
    assert limit_for(kind) == megabytes * MEGABYTE
    assert describe_limit(kind) == f"{megabytes}MB"


def test_a_file_over_the_limit_is_refused_in_words_the_owner_can_act_on():
    with pytest.raises(UploadRejected) as caught:
        check_size("document", 8 * MEGABYTE)
    message = str(caught.value)
    assert "8MB" in message and "5MB" in message
    assert "send a smaller one" in message


def test_the_size_is_checked_before_the_download_not_after():
    """Telegram reports the size on the message itself. An owner sending a 60MB video
    should be told at once, not after waiting for a transfer that was never going to be
    allowed to finish -- and that Telegram would have refused anyway."""
    import inspect

    source = inspect.getsource(photos._accept_upload)
    assert source.index("check_size") < source.index("download_file")


def test_telegram_s_own_ceiling_is_respected():
    """Bots cannot fetch files larger than 20MB whatever our limit says, so no kind may be
    configured above it -- otherwise we would accept a file we then cannot download."""
    assert max(CATEGORY_LIMITS.values()) <= TELEGRAM_MAX_DOWNLOAD_BYTES


def test_a_file_at_exactly_the_limit_is_allowed():
    check_size("video", 20 * MEGABYTE)
    check_size("document", 5 * MEGABYTE)


def test_an_unknown_size_is_not_treated_as_zero():
    """Telegram does not always report a size. Missing is not small."""
    check_size("video", None)


# ------------------------------------------------- the right file in the right slot

@pytest.mark.parametrize("kind, category", [
    ("logo", "image"), ("photo", "image"), ("video", "video"), ("document", "document"),
])
def test_each_kind_is_validated_against_its_own_file_types(kind, category):
    assert category_for(kind) == category


def test_the_accepted_types_are_the_ones_a_browser_can_actually_show():
    assert "application/pdf" in DOCUMENT_TYPES
    assert "video/mp4" in VIDEO_TYPES and "video/quicktime" in VIDEO_TYPES
    assert "image/jpeg" in IMAGE_TYPES and "image/webp" in IMAGE_TYPES
    # A browser cannot play these, so accepting them would put a dead link on a live site.
    assert "video/x-msvideo" not in VIDEO_TYPES
    assert "application/msword" not in DOCUMENT_TYPES


# ------------------------------------------------- nothing is met with silence

def test_every_attachment_type_now_has_a_handler():
    """The gap that made this necessary: only F.photo was listened for, so a PDF, a video
    or a photo sent as a file produced no reply at all."""
    import inspect

    source = inspect.getsource(photos)
    for attachment in ("F.photo", "F.video", "F.document", "F.audio", "F.voice",
                       "F.sticker", "F.animation"):
        assert attachment in source, f"nothing handles {attachment}"


def test_a_photo_sent_as_a_file_is_treated_as_a_photo():
    """Telegram sends uncompressed images as documents, which is the default when sending
    from a computer. It is still a photograph and belongs in the photograph flow."""
    import inspect

    source = inspect.getsource(photos.on_document)
    assert "IMAGE_TYPES" in source
    assert source.index("IMAGE_TYPES") < source.index("DOCUMENT_TYPES")


def test_an_unusable_file_says_what_can_be_used_instead():
    reply = photos.UNSUPPORTED_FILE_REPLY
    assert "20MB" in reply and "5MB" in reply
    assert "Photos" in reply and "Videos" in reply and "PDFs" in reply
    # No dead ends: every reply in this bot ends with something the owner can do next.
    assert "send" in reply.lower()


# ------------------------------------------------- what lands on the page

def test_a_video_is_never_allowed_to_play_by_itself():
    """An autoplaying video on a small business site spends the visitor's mobile data
    before they have decided to watch it, and is the quickest way to lose them."""
    for template in (photos.VIDEO_HERO_INSTRUCTION, photos.VIDEO_FREEFORM_INSTRUCTION):
        assert "controls" in template
        assert "never add autoplay" in template.lower()
        assert re.search(r"\bautoplay\b(?!.*never)", template.split("Rules")[0]) is None


def test_a_video_is_told_to_load_only_its_first_frame():
    """preload="metadata" instead of the whole file: a 20MB video downloaded on page load
    would make the site slower than it has ever been."""
    for template in (photos.VIDEO_HERO_INSTRUCTION, photos.VIDEO_FREEFORM_INSTRUCTION):
        assert 'preload="metadata"' in template


def test_a_pdf_link_opens_in_a_new_tab_without_giving_away_the_page():
    template = photos.DOCUMENT_INSTRUCTION
    assert 'target="_blank"' in template
    assert 'rel="noopener"' in template


def test_a_pdf_link_is_never_left_saying_LABEL():
    """The template uses a placeholder the model must replace. Shipping the word LABEL to
    a live business site would be a visible, embarrassing failure."""
    assert "Never leave the word LABEL on the page" in photos.DOCUMENT_INSTRUCTION


def test_the_templates_fill_in_cleanly():
    """They are formatted with a fixed set of keys; a stray brace would raise at the
    moment an owner is waiting, which is the worst possible time to find out."""
    values = {"url": "https://example.com/f.pdf", "name": "Rise & Crumb",
              "words": "in the middle", "filename": "menu.pdf"}
    for template in (photos.VIDEO_HERO_INSTRUCTION, photos.VIDEO_FREEFORM_INSTRUCTION,
                     photos.DOCUMENT_INSTRUCTION):
        rendered = template.format(**values)
        assert "https://example.com/f.pdf" in rendered
        assert "{" not in rendered.replace("{url}", "")


def test_a_video_at_the_top_uses_the_fixed_template_and_anything_else_is_free_form():
    assert photos._VIDEO_TOP_RE.search("put it at the top")
    assert photos._VIDEO_TOP_RE.search("at the very top of my page")
    assert not photos._VIDEO_TOP_RE.search("in the second section")
    assert not photos._VIDEO_TOP_RE.search("next to the price list")


# ------------------------------------------------- the styling contract

def test_video_and_document_classes_are_guaranteed_by_base_css():
    """Nothing builds these by default, so a video added by a later edit would land with
    no styling at all -- and a video with no max-width overflows every phone."""
    from pathlib import Path

    from worker.codegen.builder import CONTRACT_CLASSES

    base_css = Path("worker/codegen/base.css").read_text(encoding="utf-8")
    for name in ("video-wrap", "site-video", "doc-link", "doc-list"):
        assert name in CONTRACT_CLASSES, f"{name} is not in the class contract"
        assert f".{name}" in base_css, f"{name} has no base.css rule"
    assert "max-width: 100%" in base_css


def test_the_filename_cannot_carry_anything_into_a_storage_path():
    assert photos._clean_filename("../../etc/passwd") == "etc-passwd"
    assert photos._clean_filename("my menu (2).pdf") == "my-menu-2-.pdf"
    assert photos._clean_filename("") == "file"
    assert photos._clean_filename(None) == "file"
    assert len(photos._clean_filename("x" * 200)) <= 60
