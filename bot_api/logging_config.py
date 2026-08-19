"""JSON logging with per-run correlation, shared by the bot and the worker.

Two problems this solves. First, logging was effectively unconfigured outside
scripts/run_polling.py, so most of the worker's own log calls never appeared anywhere.
Second, the messages that did appear were prose -- readable one at a time, but impossible
to filter or count ("which stage fails most?", "how many tokens did this site cost?").

Every record is one JSON object with named fields, and everything emitted during a single
pipeline run carries the same `run_id`, so a build can be followed across generate ->
sandbox -> deploy -> notify even when runs overlap.

Stdlib only, matching the rest of the project's dependency habits.
"""
import contextvars
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

# Attributes LogRecord always carries; anything else was passed via `extra=` and is
# therefore one of our own structured fields.
_STANDARD = frozenset(
    "name msg args levelname levelno pathname filename module exc_info exc_text stack_info "
    "lineno funcName created msecs relativeCreated thread threadName processName process "
    "taskName message asctime".split()
)

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_run_fields: contextvars.ContextVar[dict] = contextvars.ContextVar("run_fields", default={})


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }

        run_id = _run_id.get()
        if run_id:
            payload["run_id"] = run_id
            payload.update(_run_fields.get())

        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so a UUID or datetime in a field can never crash logging itself.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(service: str) -> None:
    """Send JSON logs to stdout. Safe to call more than once."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at INFO and drown out our own events.
    for noisy in ("httpx", "httpcore", "aiogram.event", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def start_run(**fields: Any) -> str:
    """Begin a correlated run; every later log call inherits `run_id` plus `fields`."""
    run_id = uuid.uuid4().hex[:8]
    _run_id.set(run_id)
    _run_fields.set({k: v for k, v in fields.items() if v is not None})
    return run_id


def add_run_fields(**fields: Any) -> None:
    """Attach more context once it's known (e.g. the version number, after insert)."""
    merged = dict(_run_fields.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    _run_fields.set(merged)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event.

    `event` is a stable dotted name (build.started, stage.completed, build.failed) so it
    can be filtered on, while the human-readable detail stays in the fields.
    """
    logger.log(level, event, extra={"event": event, **fields})
