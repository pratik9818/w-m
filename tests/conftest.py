"""Keep the suite offline.

`build_site` now looks for photographs, and that reaches Pexels for real as soon as a key
is present in .env -- which is how a wiring test that had always run offline started
making network calls and counting a live model's tokens. Photo lookup is skipped entirely
when no key is configured, so clearing it here restores the default that every test other
than tests/test_photos.py (which stubs the lookup explicitly) relies on.
"""

import pytest

from bot_api.config import get_settings


@pytest.fixture(autouse=True)
def _no_stock_photo_lookups(monkeypatch):
    monkeypatch.setattr(get_settings(), "pexels_api_key", "", raising=False)


@pytest.fixture(autouse=True)
def _no_analytics_calls(monkeypatch):
    """Same reasoning, for Cloudflare Web Analytics.

    Provisioning a beacon reaches Cloudflare from inside the deploy path, and the deploy
    tests patch `deploy.httpx` -- not `web_analytics.httpx`, which is a different module
    object -- so a deploy test would have created a real Web Analytics site on the owner's
    account every time the suite ran. Both the provisioning call and the visitor query bail
    out before any network access when there is no token, so clearing it here is the whole
    fix. Tests that mean to exercise either one stub their own settings.
    """
    monkeypatch.setattr(get_settings(), "cloudflare_api_token", "", raising=False)
