"""Browser-independent checks on generated files, run before anything expensive.

Five of the sandbox's nine checks never needed a browser -- they only ever looked at the
markup. Running them here means a build with a trivial defect fails in milliseconds and
can be repaired *before* a Daytona container is created, instead of after ~40-90s.

That ordering is the whole point: three real builds were discarded at 6/7 and 7/8 checks
over a single empty `mailto:`, having already paid for the sandbox.

Emits the same {"name", "passed", "detail"} shape as worker/tasks/sandbox.py so both
sources of truth feed one reporting and repair path.
"""
import re

from worker.codegen.html_check import html_problems

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<script\b", re.IGNORECASE)
HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
ID_RE = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CONTACT_HREF_RE = re.compile(r"^(tel:|mailto:)\S+$")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HEADING_RE = re.compile(r"<h[12]\b", re.IGNORECASE)
IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

# Matches worker/tasks/sandbox.py -- a page that renders but says almost nothing is a
# defect, not a pass.
MIN_PAGE_WORDS = 150


def _pages(files: dict[str, str]) -> list[str]:
    return sorted((n for n in files if n.endswith(".html")), key=lambda n: n != "index.html")


def validate_files(files: dict[str, str]) -> list[dict]:
    """Run every check that can be answered from the markup alone."""
    pages = _pages(files)
    checks: list[dict] = []

    malformed = [f"{n}: {p}" for n in pages for p in html_problems(files[n])]
    checks.append({"name": "html_well_formed", "passed": not malformed, "detail": malformed})

    scripts = [n for n in pages if SCRIPT_RE.search(files[n])]
    checks.append({"name": "no_script_tags", "passed": not scripts, "detail": scripts})

    bad_contact = [
        f"{n}: {href!r}"
        for n in pages
        for href in HREF_RE.findall(files[n])
        if href.startswith(("tel:", "mailto:")) and not CONTACT_HREF_RE.match(href)
    ]
    checks.append({"name": "contact_links_valid", "passed": not bad_contact, "detail": bad_contact})

    broken_internal = []
    for name in pages:
        ids = set(ID_RE.findall(files[name]))
        for href in HREF_RE.findall(files[name]):
            target, _, anchor = href.partition("#")
            target = target.split("?")[0]
            if target:
                if "://" in target or ":" in target.split("/")[0]:
                    continue
                if target.lstrip("./") not in files:
                    broken_internal.append(f"{name} -> {href!r}")
            elif anchor and anchor not in ids:
                # A landing page's whole menu is same-page anchors, so a missing id means
                # the nav silently does nothing when clicked.
                broken_internal.append(f"{name} -> {href!r} (no element with that id)")
    checks.append(
        {"name": "internal_links_valid", "passed": not broken_internal, "detail": broken_internal}
    )

    thin = []
    for name in pages:
        html = files[name]
        title = TITLE_RE.search(html)
        words = len(TAG_RE.sub(" ", html).split())
        if not (title and title.group(1).strip()) or not HEADING_RE.search(html) or words < MIN_PAGE_WORDS:
            thin.append(f"{name}: title={bool(title)}, words={words}")
    checks.append({"name": "content_present", "passed": not thin, "detail": thin})

    empty_img = [
        f"{n}: <img> with empty src" for n in pages if any(not s.strip() for s in IMG_SRC_RE.findall(files[n]))
    ]
    checks.append({"name": "images_have_src", "passed": not empty_img, "detail": empty_img})

    return checks


def failed(checks: list[dict]) -> list[dict]:
    return [c for c in checks if not c["passed"]]


def files_needing_repair(checks: list[dict], files: dict[str, str]) -> dict[str, list[str]]:
    """Map filename -> the problems found in it, for a targeted repair prompt.

    Details are formatted "<filename>: <problem>" by the checks above, so the filename is
    recoverable without threading it separately.
    """
    per_file: dict[str, list[str]] = {}
    for check in failed(checks):
        details = check["detail"] if isinstance(check["detail"], list) else [str(check["detail"])]
        for detail in details:
            text = str(detail)
            name = text.split(":", 1)[0].split(" ")[0].strip()
            if name in files:
                per_file.setdefault(name, []).append(f"{check['name']}: {text}")
            else:
                # Not attributable to one file -- attach to every page so it still gets seen.
                for page in _pages(files):
                    per_file.setdefault(page, []).append(f"{check['name']}: {text}")
    return per_file
