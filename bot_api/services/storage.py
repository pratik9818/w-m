import uuid

import httpx

from bot_api.config import get_settings

BUCKET = "business-media"

# What each kind of upload is allowed to be, and how large. Separate limits rather than one
# ceiling because the constraints are genuinely different: a photograph is the thing the
# page is built around and deserves room, while a PDF menu that runs to 5MB is a scan
# nobody on a phone will wait for.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime"}
DOCUMENT_TYPES = {"application/pdf"}

MEGABYTE = 1024 * 1024

CATEGORY_LIMITS = {
    "image": 20 * MEGABYTE,
    "video": 20 * MEGABYTE,
    "document": 5 * MEGABYTE,
}

# Telegram will not hand a bot a file larger than this, whatever our own limit says. A
# 40MB video sent from a phone cannot be downloaded at all, so the size has to be refused
# before the download is attempted -- otherwise the owner waits for a failure that was
# knowable immediately.
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * MEGABYTE

CATEGORY_TYPES = {
    "image": IMAGE_TYPES,
    "video": VIDEO_TYPES,
    "document": DOCUMENT_TYPES,
}

# Which upload kinds are which sort of file. `kind` is what the media row is called and
# what decides where it lands on the page; the category is what decides whether we accept
# it at all.
KIND_CATEGORY = {
    "logo": "image",
    "photo": "image",
    "video": "video",
    "document": "document",
}

FRIENDLY_TYPES = {
    "image": "a JPEG, PNG or WebP image",
    "video": "an MP4, WebM or MOV video",
    "document": "a PDF",
}


class UploadRejected(Exception):
    """The file cannot be accepted. The message is written to be shown to the owner."""


def category_for(kind: str) -> str:
    return KIND_CATEGORY.get(kind, "image")


def limit_for(kind: str) -> int:
    return CATEGORY_LIMITS[category_for(kind)]


def describe_limit(kind: str) -> str:
    """"20MB" -- for telling an owner what they can send, before they send it."""
    return f"{limit_for(kind) // MEGABYTE}MB"


def check_size(kind: str, size_bytes: int | None) -> None:
    """Refuse an oversized file before downloading it. Raises UploadRejected.

    Called with the size Telegram reports on the message, so an owner who sends a 60MB
    video is told immediately rather than after a download that was never going to work.
    """
    if not size_bytes:
        return
    limit = limit_for(kind)
    if size_bytes > limit:
        raise UploadRejected(
            f"That {kind} is {size_bytes / MEGABYTE:.0f}MB, and I can only take up to "
            f"{describe_limit(kind)}. Could you send a smaller one?"
        )
    if size_bytes > TELEGRAM_MAX_DOWNLOAD_BYTES:
        raise UploadRejected(
            "Telegram won't let me download files larger than 20MB. Could you send a "
            "smaller version?"
        )


async def upload_media(
    business_id: uuid.UUID, kind: str, filename: str, content: bytes, content_type: str
) -> dict:
    category = category_for(kind)
    allowed = CATEGORY_TYPES[category]
    if content_type not in allowed:
        raise UploadRejected(
            f"I can't use that file type. Please send {FRIENDLY_TYPES[category]}."
        )
    if len(content) > CATEGORY_LIMITS[category]:
        raise UploadRejected(
            f"That file is too large — please keep {kind}s under {describe_limit(kind)}."
        )

    settings = get_settings()
    object_path = f"{business_id}/{kind}/{uuid.uuid4()}-{filename}"
    upload_url = f"{settings.supabase_url}/storage/v1/object/{BUCKET}/{object_path}"

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            upload_url,
            content=content,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
        )
        response.raise_for_status()

    public_url = f"{settings.supabase_url}/storage/v1/object/public/{BUCKET}/{object_path}"
    return {"storage_path": object_path, "url": public_url}
