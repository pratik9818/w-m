"""Not paying a model to fix things that were never broken.

The day analytics shipped, every edit quietly became about 45% more expensive. The
Cloudflare beacon is injected at deploy, so from the next deploy onward it was sitting in
the stored files -- and it cannot reach Cloudflare from inside an isolated sandbox. Every
page logged a failed request, `no_console_errors` failed, and a four-page repair was
commissioned costing 33,000 tokens and about $0.27. It fixed nothing (`fixed: 0`) and the
build passed anyway.

Nothing was wrong with the site. The sandbox was reporting a fact about its own network.

Both halves are guarded here: the beacon is not tested at all, and no third-party resource
failure is ever mistaken for a defect in the owner's page.
"""

from worker.tasks.web_analytics import BEACON_SRC, inject_beacon, strip_beacon

PAGE = (
    "<html><head><title>T</title></head><body>\n"
    "<h1>Rise &amp; Crumb</h1>\n"
    "</body></html>\n"
)


# ------------------------------------------------- the beacon is never sandbox-tested

def test_the_beacon_is_removed_before_a_page_is_tested():
    beaconed = inject_beacon({"index.html": PAGE}, "tok123")
    assert BEACON_SRC in beaconed["index.html"]

    stripped = strip_beacon(beaconed)
    assert BEACON_SRC not in stripped["index.html"]
    assert "tok123" not in stripped["index.html"]


def test_stripping_leaves_the_rest_of_the_page_exactly_as_it_was():
    """It must remove the measurement and nothing else -- the sandbox compares
    screenshots before and after, so any other change would read as a visual difference."""
    round_tripped = strip_beacon(inject_beacon({"index.html": PAGE}, "tok123"))
    assert round_tripped["index.html"] == PAGE


def test_stripping_a_page_that_never_had_one_changes_nothing():
    files = {"index.html": PAGE, "style.css": "body{}"}
    assert strip_beacon(files) == files


def test_stylesheets_are_left_alone():
    files = {"style.css": "/* static.cloudflareinsights.com mentioned in a comment */"}
    assert strip_beacon(files) == files


def test_strip_and_inject_are_reversible_so_deploy_puts_it_back():
    """The pipeline strips before testing and deploy injects again. If those two ever
    disagreed, sites would quietly stop counting visitors."""
    original = {"index.html": PAGE}
    cycled = inject_beacon(strip_beacon(inject_beacon(original, "tok123")), "tok123")
    assert cycled["index.html"].count(BEACON_SRC) == 1


def test_empty_input_is_handled():
    assert strip_beacon(None) is None
    assert strip_beacon({}) == {}


# ------------------------------------------------- third-party failures are not defects

class FakeMessage:
    def __init__(self, text, url, kind="error"):
        self.text = text
        self.type = kind
        self.location = {"url": url}


def _record(messages, base_url="http://localhost:3000"):
    """Mirrors _record_console in sandbox.py, which cannot be imported without a browser."""
    recorded = []
    for msg in messages:
        if msg.type != "error":
            continue
        source = (msg.location or {}).get("url") or ""
        if source and not source.startswith(base_url):
            continue
        recorded.append(msg.text)
    return recorded


def test_a_cdn_that_cannot_be_reached_is_not_the_owner_s_problem():
    """The parser explicitly allows a page to load an icon pack or CSS library from a CDN.
    None of them resolve inside the sandbox."""
    recorded = _record([
        FakeMessage("Failed to load resource: net::ERR_NAME_NOT_RESOLVED",
                    "https://static.cloudflareinsights.com/beacon.min.js"),
        FakeMessage("Failed to load resource", "https://cdn.jsdelivr.net/npm/some-lib.js"),
    ])
    assert recorded == []


def test_an_error_in_the_page_s_own_code_is_still_caught():
    """The check must keep doing its job. A stray bracket in the site's own script is a
    real defect and the reason this check exists at all."""
    recorded = _record([
        FakeMessage("Uncaught SyntaxError: Unexpected token ')'",
                    "http://localhost:3000/index.html"),
    ])
    assert recorded == ["Uncaught SyntaxError: Unexpected token ')'"]


def test_an_error_with_no_source_is_kept_rather_than_assumed_harmless():
    """Some errors carry no location. Dropping those would be the same mistake in the
    other direction -- silence about a real fault."""
    recorded = _record([FakeMessage("Something went wrong", "")])
    assert recorded == ["Something went wrong"]


def test_warnings_and_logs_are_not_errors():
    recorded = _record([
        FakeMessage("just a log", "http://localhost:3000/index.html", kind="log"),
        FakeMessage("a warning", "http://localhost:3000/index.html", kind="warning"),
    ])
    assert recorded == []


# ------------------------------------------------- the wiring

def test_the_pipeline_strips_before_it_tests():
    """Applied after the sandbox call it would achieve nothing, and the 33,000-token
    repair would carry on being commissioned every time."""
    import inspect

    from worker.tasks import generate

    source = inspect.getsource(generate.run_generation_pipeline)
    assert "strip_beacon(files)" in source
    assert source.index("strip_beacon(files)") < source.index("await sandbox_test(")
