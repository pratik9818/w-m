"""Run the edit parser against frozen fixtures and score what it decided.

Usage (from the repo root):
    python evals/run_evals.py                     # every case, once
    python evals/run_evals.py --case already-tall-and-bold
    python evals/run_evals.py --repeat 3          # sample a stochastic model

Exits non-zero if any case fails, so it can gate a prompt change in CI. Each case costs
one model call (~7.5k tokens), so the whole suite is roughly one edit's worth of quota.

Nothing here touches the database, Redis, or a live site: fixtures are files, and the
business is a transient ORM object that is never added to a session.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import Business, Media, Service  # noqa: E402
from evals.cases import CASES  # noqa: E402
from bot_api.services.analytics import looks_like_a_traffic_question  # noqa: E402
from evals.promote import load_regressions  # noqa: E402
from worker.codegen.css_values import effective_styles, html_classes  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Words an owner has never seen and cannot answer a question about. A clarify that uses
# one of these is a failure however sensible the question is -- a real owner was asked to
# paste "the current markup of the FAQ section".
JARGON = re.compile(
    r"\b(html|css|javascript|markup|selector|class name|attribute|<[a-z]+>|div|anchor|"
    r"stylesheet|index\.html|style\.css|about\.html|services\.html|contact\.html)\b",
    re.IGNORECASE,
)
_PX = re.compile(r"^(\d*\.?\d+)px$")
_RANGE = re.compile(r"renders between [\d.]+px and ([\d.]+)px")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def business_from(fixture: dict) -> Business:
    """A transient Business carrying the fixture's data -- never persisted."""
    business = Business(**fixture["business"])
    business.services = [Service(**row) for row in fixture["services"]]
    business.media = [Media(**row) for row in fixture["media"]]
    return business


def _numeric_px(value: str) -> float | None:
    match = _PX.match((value or "").strip())
    return float(match.group(1)) if match else None


def _current(value: str) -> tuple[str, float | None]:
    """(the literal value, the pixel size a change has to beat).

    The digest annotates a responsive value with what it renders as -- "clamp(1.75rem,
    4vw, 3rem) -- renders between 28px and 48px" -- and 48 is the number that matters. It
    is the one the heading was actually showing when a "bigger" request set it to 32px.
    """
    if match := _RANGE.search(value or ""):
        return value, float(match.group(1))
    head = (value or "").split(" ")[0].rstrip(";")
    return head, _numeric_px(head)


def _check_beats(op: dict, fixture: dict, rule: dict) -> list[str]:
    selector, prop = rule["selector"], rule["property"]
    changes = op.get("changes") or []
    mine = [c for c in changes if str(c.get("selector", "")).endswith(selector)
            and c.get("property") == prop]
    if op["operation"] != "set_style":
        # A patch_site instruction is prose; all we can insist on is that it does not
        # restate the value the site already has.
        styles, _ = effective_styles(fixture["files"]["style.css"])
        current_value, _ = _current(styles.get(selector.lstrip("."), {}).get(prop, ""))
        instruction = str(op.get("instruction") or "")
        if current_value and re.search(rf"\b{re.escape(current_value)}\b", instruction):
            return [f"restates the current {prop} ({current_value}) instead of changing it"]
        return []
    if not mine:
        return [f"no change to {selector} {prop}"]

    styles, _ = effective_styles(fixture["files"]["style.css"])
    literal, old_px = _current(styles.get(selector.lstrip("."), {}).get(prop, ""))
    new_value = str(mine[-1].get("value", ""))
    if new_value.strip() == literal.strip():
        return [f"{selector} {prop} set to the value it already has ({new_value})"]

    new_px = _numeric_px(new_value)
    if old_px is not None and new_px is not None and new_px <= old_px:
        return [f"{selector} {prop} {new_value} is not bigger than the current {literal}"]
    return []


def _known_selectors(fixture: dict) -> set[str]:
    styles, _ = effective_styles(fixture["files"]["style.css"])
    return set(styles) | set(html_classes(fixture["files"]))


def evaluate(case: dict, op: dict, fixture: dict) -> list[str]:
    """Every way this parse fell short, as plain sentences. Empty means it passed."""
    problems: list[str] = []
    # `from` carries the value already in force -- that is the whole point of the field --
    # so it must not count as the operation "asking for" that value.
    blob = json.dumps(_without_from(op), ensure_ascii=False)

    expected = case.get("expect_operation")
    if expected and op["operation"] not in expected:
        problems.append(f"chose {op['operation']}, wanted one of {sorted(expected)}")

    forbidden = case.get("forbid_operation")
    if forbidden and op["operation"] in forbidden:
        problems.append(f"chose {op['operation']}, which is the failure this case records")

    # For a case promoted from a real "the owner asked again" failure. We know this exact
    # answer did not satisfy them; we do not know which one would have, so landing on it a
    # second time is the only thing worth failing on.
    if (previous := case.get("forbid_identical")) is not None:
        if _without_from(op) == _without_from(previous):
            problems.append("produced the same operation that the owner rejected before")

    for pattern in case.get("forbid_text", []):
        if re.search(pattern, blob, re.IGNORECASE):
            problems.append(f"says {pattern!r}, which it must not")

    for pattern in case.get("require_text", []):
        if not re.search(pattern, blob, re.IGNORECASE):
            problems.append(f"never mentions {pattern!r}")

    if rule := case.get("require_beats"):
        problems += _check_beats(op, fixture, rule)

    # --- rules that apply to every case ---------------------------------------------
    if op["operation"] == "clarify" and (found := JARGON.search(op.get("question", ""))):
        problems.append(f"asked the owner about {found.group(0)!r}")

    known = _known_selectors(fixture)
    for change in op.get("changes") or []:
        name = str(change.get("selector", "")).split(".")[-1]
        if name and name not in known:
            problems.append(f"invented .{name}, which is not on this site")
        stated, wanted = str(change.get("from") or ""), str(change.get("value") or "")
        if stated and stated.strip() == wanted.strip():
            problems.append(
                f"{change.get('selector')} {change.get('property')} set to {wanted}, "
                "which is what it already is"
            )

    return problems


def _without_from(op: dict) -> dict:
    """The operation minus the fields that describe the site's current state."""
    stripped = dict(op)
    if isinstance(stripped.get("changes"), list):
        stripped["changes"] = [
            {key: value for key, value in change.items() if key != "from"}
            for change in stripped["changes"]
            if isinstance(change, dict)
        ]
    return stripped


async def run(cases: list[dict], repeat: int) -> int:
    from bot_api.services.nl_edit import parse_edit_message

    fixtures = {name: load_fixture(name) for name in {c["fixture"] for c in cases}}
    failures = 0
    total_tokens = 0

    for case in cases:
        fixture = fixtures[case["fixture"]]
        business = business_from(fixture)
        outcomes: list[list[str]] = []
        chosen: list[dict] = []
        for _ in range(repeat):
            try:
                # Mirror the handler's routing, not just the parser. In production a
                # question about visitors is answered from Cloudflare before the parser
                # is ever called, and a corpus that skips that step reports a failure the
                # owner could not have experienced -- while charging a model call to do
                # it. The first promoted case to fail here failed for exactly that reason.
                if looks_like_a_traffic_question(case["message"]):
                    outcomes.append(evaluate(case, {"operation": "answer_traffic"}, fixture))
                    continue
                op, usage = await parse_edit_message(
                    case["message"], business, case.get("context"), fixture["files"]
                )
                total_tokens += usage["input_tokens"] + usage["output_tokens"]
                problems = evaluate(case, op, fixture)
                outcomes.append(problems)
                if problems:
                    chosen.append(op)
            except Exception as exc:  # a crash is a failure, not an excuse
                outcomes.append([f"parser raised {type(exc).__name__}: {exc}"])

        passes = sum(1 for problems in outcomes if not problems)
        status = "PASS" if passes == repeat else ("FLAKY" if passes else "FAIL")
        if passes < repeat:
            failures += 1
        tally = f" [{passes}/{repeat}]" if repeat > 1 else ""
        print(f"{status:<5} {case['id']}{tally}")
        if case.get("note") and passes < repeat:
            print(f"      guards: {case['note']}")
        for problems in outcomes:
            for problem in problems:
                print(f"      - {problem}")
        for op in chosen:
            print(f"      got: {json.dumps(op, ensure_ascii=False)}")

    print(f"\n{len(cases) - failures}/{len(cases)} cases passed, {total_tokens:,} tokens")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Score the edit parser against real messages.")
    parser.add_argument("--case", action="append", help="run only these case ids")
    parser.add_argument("--repeat", type=int, default=1,
                        help="parse each case N times; models are stochastic")
    parser.add_argument("--handwritten-only", action="store_true",
                        help="skip the cases promoted from real failures")
    args = parser.parse_args()

    # Hand-written cases first, then everything the bot has failed at in production.
    # evals/promote.py grows the second list; nobody types those out.
    cases = list(CASES) + ([] if args.handwritten_only else load_regressions())
    if args.case:
        wanted = set(args.case)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            sys.exit(f"No such case: {', '.join(sorted(missing))}")

    sys.exit(asyncio.run(run(cases, args.repeat)))


if __name__ == "__main__":
    main()
