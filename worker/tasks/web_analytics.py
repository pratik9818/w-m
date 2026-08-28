"""Give a deployed site something that counts its visitors.

Nothing in this project was ever counting. An owner asking "how many people looked at my
site today?" -- which is the first question anyone asks once their site is live, and the
only one that tells them whether any of this was worth doing -- could not be answered at
all, because the number did not exist anywhere.

Cloudflare will count it for free, but only for pages carrying its Web Analytics beacon,
and the beacon needs a token issued per hostname. That token is issued here, once, on the
first deploy, and stored on the business so every later deploy reuses it. Reissuing would
be worse than not counting: a new token is a new site as far as Cloudflare is concerned,
and the owner's history would silently reset to zero.

Two rules hold this module together:

  1. **Analytics must never break a deploy.** Every function here swallows its own
     failures and returns None. A site that publishes without a beacon loses a number
     nobody has yet; a site that fails to publish because the analytics API had a bad
     afternoon has lost the owner their website. The asymmetry is not close.
  2. **The beacon goes in exactly once.** Injection is keyed on the script's own src, so
     re-running it over already-beaconed HTML -- which happens on every redeploy, because
     the stored bytes come back through here -- changes nothing.
"""
import logging
import re

import httpx

from bot_api.config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"

# Cloudflare serves the beacon from here. Matched against page HTML to decide whether a
# page already carries one, so it must stay in step with BEACON_TEMPLATE below.
BEACON_SRC = "https://static.cloudflareinsights.com/beacon.min.js"

# `defer` matters: this is a measurement script on a small business's page, and it must
# never be in front of the content the visitor came for.
BEACON_TEMPLATE = (
    '<script defer src="' + BEACON_SRC + '" '
    'data-cf-beacon=\'{{"token": "{token}"}}\'></script>'
)


async def ensure_analytics_site(host: str) -> tuple[str, str] | None:
    """Get (site_tag, site_token) for `host`, creating the Web Analytics site if needed.

    `host` is the bare hostname -- "rise-and-crumb.pages.dev", no scheme.

    Returns None on any failure, including the common one: an API token scoped only for
    Pages, which is what every deployment of this project had before analytics existed.
    That case is logged at warning and is otherwise silent -- the site still deploys, it
    just does not count.
    """
    settings = get_settings()
    if not settings.cloudflare_api_token or not settings.cloudflare_account_id:
        return None

    headers = {"Authorization": f"Bearer {settings.cloudflare_api_token}"}
    account_id = settings.cloudflare_account_id
    url = f"{API_BASE}/accounts/{account_id}/rum/site_info"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Look before creating. Cloudflare does NOT reject a second site for a host it
            # already has -- verified live, where two calls a second apart produced two
            # site tags for looksalon.pages.dev. So "create and handle the conflict" has no
            # conflict to handle: it silently splits the site's history in two, and the
            # owner's visitor numbers restart at zero with no error anywhere.
            existing = await _find_existing_site(client, headers, account_id, host)
            if existing is not None:
                return existing

            # auto_install is for orange-clouded zones, where Cloudflare can inject the
            # beacon itself. A *.pages.dev host is not one, so it is off and the snippet
            # goes into the HTML here instead.
            resp = await client.post(url, headers=headers, json={"host": host, "auto_install": False})
            data = resp.json()
            if resp.status_code == 200 and data.get("success"):
                result = data["result"]
                return result["site_tag"], result["site_token"]

            # Two deploys racing: the other one created it between our lookup and our
            # create. Its site is as good as ours would have been.
            existing = await _find_existing_site(client, headers, account_id, host)
            if existing is not None:
                return existing

            logger.warning(
                "analytics.provision_failed",
                extra={"event": "analytics.provision_failed", "host": host,
                       "status": resp.status_code, "errors": str(data.get("errors"))[:300]},
            )
            return None
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning(
            "analytics.provision_error",
            extra={"event": "analytics.provision_error", "host": host, "error": str(exc)[:200]},
        )
        return None


# A site added through the Cloudflare dashboard has its host stored as a regex fragment --
# "(xtravu.pages.dev)$" was found on a real account, next to a plain "looksalon.pages.dev"
# created through this API. Comparing the raw strings misses the dashboard-made ones, which
# is the case where a duplicate matters most: the owner already has history under it.
_HOST_NOISE = re.compile(r"[()$^\\]")


def _normalise_host(value: str | None) -> str:
    return _HOST_NOISE.sub("", (value or "").strip()).lower()


async def _find_existing_site(
    client: httpx.AsyncClient, headers: dict, account_id: str, host: str
) -> tuple[str, str] | None:
    """The Web Analytics site already registered for `host`, if there is one."""
    resp = await client.get(
        f"{API_BASE}/accounts/{account_id}/rum/site_info/list",
        headers=headers,
        params={"per_page": 100},
    )
    data = resp.json()
    if resp.status_code != 200 or not data.get("success"):
        return None

    wanted = _normalise_host(host)
    for site in data.get("result") or []:
        rules = site.get("rules") or []
        hosts = {_normalise_host(site.get("host"))}
        hosts |= {_normalise_host(rule.get("host")) for rule in rules}
        if wanted in hosts and site.get("site_tag") and site.get("site_token"):
            return site["site_tag"], site["site_token"]
    return None


_BEACON_TAG_RE = re.compile(
    r"[ \t]*<script[^>]*static\.cloudflareinsights\.com[^>]*>\s*</script>\s*\n?",
    re.IGNORECASE,
)


def strip_beacon(files: dict[str, str] | None) -> dict[str, str] | None:
    """The same pages without the analytics beacon.

    Used before the sandbox runs. The beacon is instrumentation, not content: it is added
    at deploy and it can never load inside an isolated sandbox, so the browser logs a
    failed request on every page, `no_console_errors` fails, and a repair is commissioned
    for all four pages to fix a script that was never broken.

    That cost 33,000 tokens and about $0.27 per edit, fixed nothing, and the build then
    passed anyway -- a self-inflicted tax introduced the day analytics shipped. Testing a
    page as the visitor will see it means testing the page, not our measurement of it.
    """
    if not files:
        return files
    return {
        name: (_BEACON_TAG_RE.sub("", body) if name.endswith(".html") else body)
        for name, body in files.items()
    }


def inject_beacon(files: dict[str, str], site_token: str | None) -> dict[str, str]:
    """Put the analytics beacon in every page, if there is a token and it isn't there yet.

    Returns a new dict. Idempotent by design: redeploys hand back the stored bytes, which
    already contain a beacon, and a second copy would double-count every visit.
    """
    if not site_token:
        return files

    snippet = BEACON_TEMPLATE.format(token=site_token)
    beaconed = dict(files)
    for filename, content in files.items():
        if not filename.endswith(".html") or BEACON_SRC in content:
            continue
        if "</body>" not in content:
            continue
        # Last thing before </body>: the page is already rendered and interactive by the
        # time this runs, which is where a measurement script belongs.
        beaconed[filename] = content.replace("</body>", f"  {snippet}\n</body>", 1)
    return beaconed
