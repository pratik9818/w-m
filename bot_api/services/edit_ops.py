from sqlalchemy.ext.asyncio import AsyncSession

from bot_api.services.validation import EMAIL_RE, FIELD_LIMITS, THEMES
from db.models import Business, Service

BUSY_STATUSES = {"queued", "generating", "testing", "deploying"}


def is_business_busy(business: Business) -> bool:
    return business.generation_status in BUSY_STATUSES


class ValidationError(Exception):
    pass


async def apply_edit_operation(session: AsyncSession, business: Business, op: dict) -> str:
    """Apply a validated operation to `business`/its services on `session`.

    Mutates in place; caller commits. Returns a short human-readable summary for
    the confirmation reply. Raises ValidationError (with a user-facing message)
    on any invalid input -- never silently clamps or guesses.
    """
    operation = op["operation"]
    if operation == "update_business_info":
        return _apply_update_business_info(business, op)
    if operation == "add_service":
        return _apply_add_service(session, business, op)
    if operation == "update_service":
        return _apply_update_service(business, op)
    if operation == "remove_service":
        return _apply_remove_service(business, op)
    raise ValueError(f"apply_edit_operation called with a non-mutating operation: {operation}")


def _check_length(field_label: str, value: str, limit_key: str) -> None:
    limit = FIELD_LIMITS[limit_key]
    if len(value) > limit:
        raise ValidationError(
            f"That {field_label} is {len(value)} characters — I can only fit {limit}. Try something shorter?"
        )


def _apply_update_business_info(business: Business, op: dict) -> str:
    changed: list[str] = []

    if "name" in op:
        value = op["name"].strip()
        _check_length("name", value, "name")
        business.name = value
        changed.append("name")
    if "tagline" in op:
        value = op["tagline"].strip()
        _check_length("tagline", value, "tagline")
        business.tagline = value
        changed.append("tagline")
    if "about" in op:
        value = op["about"].strip()
        _check_length("about description", value, "about")
        business.about = value
        changed.append("about section")
    if "phone" in op:
        value = op["phone"].strip()
        _check_length("phone number", value, "phone")
        business.phone = value
        changed.append("phone number")
    if "email" in op:
        value = op["email"].strip()
        if not EMAIL_RE.match(value):
            raise ValidationError(f"\"{value}\" doesn't look like a valid email — what should it be?")
        _check_length("email", value, "email")
        business.email = value
        changed.append("email")
    if "address" in op:
        value = op["address"].strip()
        _check_length("address", value, "address")
        business.address = value
        changed.append("address")
    if "theme" in op:
        value = op["theme"].strip().lower()
        if value not in THEMES:
            raise ValidationError("I can only use classic, modern, or bold as a look — which one?")
        business.theme = value
        changed.append("look")
    if "hours" in op:
        value = op["hours"].strip()
        _check_length("hours", value, "hours")
        business.hours = {"display_text": value}
        changed.append("hours")

    if not changed:
        raise ValidationError("I didn't catch a specific change to make — could you tell me again?")

    return "changed your " + ", ".join(changed)


def _find_service(business: Business, name: str) -> Service:
    active = [s for s in business.services if s.is_active]
    needle = name.strip().lower()

    exact = [s for s in active if s.name.strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]

    substring = [s for s in active if needle in s.name.strip().lower()]
    matches = exact or substring
    if len(matches) == 1:
        return matches[0]

    names = ", ".join(f'"{s.name}"' for s in active) or "(none)"
    if not matches:
        raise ValidationError(f'I couldn\'t find a service called "{name}". Your current services: {names}.')
    raise ValidationError(f'"{name}" matches more than one service ({names}) — which one did you mean?')


def _apply_add_service(session: AsyncSession, business: Business, op: dict) -> str:
    name = op["name"].strip()
    _check_length("service name", name, "service_name")
    price_label = op.get("price_label")
    if price_label:
        price_label = price_label.strip()
        _check_length("price", price_label, "service_price_label")

    existing = next(
        (s for s in business.services if s.is_active and s.name.strip().lower() == name.lower()), None
    )
    if existing is not None:
        existing.price_label = price_label or existing.price_label
        return f'updated "{existing.name}"' + (f" ({price_label})" if price_label else "")

    next_order = max((s.sort_order for s in business.services), default=-1) + 1
    session.add(
        Service(
            business_id=business.id,
            name=name,
            price_label=price_label,
            sort_order=next_order,
        )
    )
    return f'added "{name}"' + (f" ({price_label})" if price_label else "")


def _apply_update_service(business: Business, op: dict) -> str:
    service = _find_service(business, op["current_name"])
    changed: list[str] = []

    new_name = op.get("new_name")
    if new_name:
        new_name = new_name.strip()
        _check_length("service name", new_name, "service_name")
        service.name = new_name
        changed.append("name")

    new_price_label = op.get("new_price_label")
    if new_price_label:
        new_price_label = new_price_label.strip()
        _check_length("price", new_price_label, "service_price_label")
        service.price_label = new_price_label
        changed.append("price")

    if not changed:
        raise ValidationError("What should I change about that service — its name or its price?")

    return f'updated "{service.name}"\'s ' + " and ".join(changed)


def _apply_remove_service(business: Business, op: dict) -> str:
    service = _find_service(business, op["name"])
    service.is_active = False
    return f'removed "{service.name}"'
