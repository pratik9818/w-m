"""Deploy generated site files to Cloudflare Pages.

The project-create and deployment-create calls are Cloudflare's official, documented
REST API. The asset-upload step (upload-token -> assets/upload) is NOT officially
documented for plain REST use (only the wrangler CLI / dashboard drag-and-drop are
documented paths) -- this implementation was reverse-engineered from community reports
and then verified live against a real Cloudflare account (a full create-project ->
upload-token -> upload-assets -> create-deployment run, followed by fetching the
resulting URL and confirming it served the exact uploaded content). Notably,
upload-token is a GET request, not POST, despite every third-party writeup assuming
POST -- this was only caught by testing against the real API. If Cloudflare changes
these undocumented endpoints in the future, the fallback is shelling out to
`wrangler pages deploy <dir> --project-name=<name>` via subprocess instead.
"""
import base64
import json

import blake3
import httpx

from bot_api.config import get_settings
from db.models import Business
from worker.codegen.seo import finalise_urls

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareDeployError(Exception):
    pass


async def deploy_to_cloudflare_pages(
    business: Business, files: dict[str, str]
) -> tuple[str, str, dict[str, str]]:
    """Deploy `files` (filename -> content) to a Cloudflare Pages project for `business`.

    Creates the project on first deploy, reuses it on subsequent ones (keyed by
    business.cf_pages_project_name so the owner's URL never changes across re-deploys).

    Returns (project_name, stable_pages_dev_url, files_as_deployed). The third is not the
    dict that came in: a first build cannot know its own address, so its canonical tags and
    sitemap are filled in here. The caller stores *these* bytes, because the version record
    has to be what is actually live -- the next edit patches against it.
    """
    settings = get_settings()
    account_id = settings.cloudflare_account_id
    project_name = business.cf_pages_project_name or business.slug

    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {settings.cloudflare_api_token}"}

        if business.cf_pages_project_name:
            subdomain = await _get_project_subdomain(client, headers, account_id, project_name)
        else:
            subdomain = await _ensure_project(client, headers, account_id, project_name)

        # The first build of a new project is written before anyone knows what Cloudflare
        # will call it -- the subdomain carries a random suffix when the name is taken, so
        # it cannot be predicted, only asked for. It is known by this line, and the pages
        # left a marker where their canonical tag belongs, so it goes in now. Every later
        # build already has the address and leaves no marker for this to find.
        files = finalise_urls(files, f"https://{subdomain}")

        manifest = {f"/{name}": _hash_file(name, content) for name, content in files.items()}
        upload_jwt = await _get_upload_jwt(client, headers, account_id, project_name)
        await _upload_assets(client, upload_jwt, files, manifest)
        await _create_deployment(client, headers, account_id, project_name, manifest)

    return project_name, f"https://{subdomain}", files


def _hash_file(filename: str, content: str) -> str:
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return blake3.blake3((encoded + extension).encode("ascii")).hexdigest()[:32]


async def _ensure_project(client: httpx.AsyncClient, headers: dict, account_id: str, project_name: str) -> str:
    resp = await client.post(
        f"{API_BASE}/accounts/{account_id}/pages/projects",
        headers=headers,
        json={"name": project_name, "production_branch": "main"},
    )
    data = resp.json()
    if resp.status_code == 200 and data.get("success"):
        return data["result"]["subdomain"]

    # A prior attempt may have created the project but crashed before saving its name
    # to the DB (4a has no retries) -- proceed with the same deterministic name.
    already_exists = any(
        "already exists" in (err.get("message") or "").lower() for err in data.get("errors", [])
    )
    if already_exists:
        return await _get_project_subdomain(client, headers, account_id, project_name)
    raise CloudflareDeployError(f"Failed to create Pages project '{project_name}': {data.get('errors')}")


async def _get_project_subdomain(
    client: httpx.AsyncClient, headers: dict, account_id: str, project_name: str
) -> str:
    """Read the project's real *.pages.dev hostname from Cloudflare.

    Never derive this from the project name: Cloudflare appends a random suffix when the
    name is already taken globally (observed live -- project "website-maker" was served at
    "website-maker-6hf.pages.dev"). Guessing it overwrote the owner's working link with a
    dead parking page on the first re-deploy.
    """
    resp = await client.get(
        f"{API_BASE}/accounts/{account_id}/pages/projects/{project_name}", headers=headers
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("success"):
        raise CloudflareDeployError(f"Failed to read Pages project '{project_name}': {data.get('errors')}")
    subdomain = (data.get("result") or {}).get("subdomain")
    if not subdomain:
        raise CloudflareDeployError(f"Pages project '{project_name}' has no subdomain yet")
    return subdomain


async def _get_upload_jwt(client: httpx.AsyncClient, headers: dict, account_id: str, project_name: str) -> str:
    resp = await client.get(
        f"{API_BASE}/accounts/{account_id}/pages/projects/{project_name}/upload-token",
        headers=headers,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("success"):
        raise CloudflareDeployError(f"Failed to get Pages upload token: {data.get('errors')}")
    return data["result"]["jwt"]


async def _upload_assets(
    client: httpx.AsyncClient, upload_jwt: str, files: dict[str, str], manifest: dict[str, str]
) -> None:
    name_by_hash = {h: name for name, h in manifest.items()}
    payload = [
        {
            "key": file_hash,
            "value": base64.b64encode(files[name_by_hash[file_hash].lstrip("/")].encode("utf-8")).decode("ascii"),
            "metadata": {"contentType": _content_type(name_by_hash[file_hash])},
            "base64": True,
        }
        for file_hash in manifest.values()
    ]
    resp = await client.post(
        f"{API_BASE}/pages/assets/upload",
        headers={"Authorization": f"Bearer {upload_jwt}"},
        json=payload,
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("success"):
        raise CloudflareDeployError(f"Failed to upload Pages assets: {data.get('errors')}")


async def _create_deployment(
    client: httpx.AsyncClient, headers: dict, account_id: str, project_name: str, manifest: dict[str, str]
) -> str:
    resp = await client.post(
        f"{API_BASE}/accounts/{account_id}/pages/projects/{project_name}/deployments",
        headers=headers,
        data={"branch": "main"},
        files={"manifest": (None, json.dumps(manifest))},
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("success"):
        raise CloudflareDeployError(f"Failed to create Pages deployment: {data.get('errors')}")
    return data["result"]["url"]


async def delete_pages_project(project_name: str) -> None:
    """Remove a Pages project so deleted sites don't consume the ~100-project free cap.

    Raises CloudflareDeployError on failure; the caller decides whether that should block
    the local delete (it shouldn't -- an orphaned Cloudflare project is untidy, whereas a
    business the owner asked to delete surviving in our DB is a real problem).
    """
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{API_BASE}/accounts/{settings.cloudflare_account_id}/pages/projects/{project_name}",
            headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"},
        )
    if resp.status_code not in (200, 404):
        raise CloudflareDeployError(f"Failed to delete Pages project '{project_name}': {resp.text[:300]}")


def _content_type(filename: str) -> str:
    if filename.endswith(".html"):
        return "text/html"
    if filename.endswith(".css"):
        return "text/css"
    if filename.endswith(".js"):
        return "text/javascript"
    # Crawlers are strict about these two in a way browsers are not: served as
    # application/octet-stream, a sitemap is downloaded rather than read, and the whole
    # point of writing one is lost silently.
    if filename.endswith(".xml"):
        return "application/xml"
    if filename.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"
