import uuid

import httpx

from bot_api.config import get_settings

BUCKET = "business-media"
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class UploadRejected(Exception):
    pass


async def upload_media(
    business_id: uuid.UUID, kind: str, filename: str, content: bytes, content_type: str
) -> dict:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadRejected(f"Unsupported file type: {content_type}. Please send a jpeg, png, or webp.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadRejected("That file is too large — please keep images under 5MB.")

    settings = get_settings()
    object_path = f"{business_id}/{kind}/{uuid.uuid4()}-{filename}"
    upload_url = f"{settings.supabase_url}/storage/v1/object/{BUCKET}/{object_path}"

    async with httpx.AsyncClient(timeout=30) as client:
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
