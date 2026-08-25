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
