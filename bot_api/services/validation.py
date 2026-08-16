import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

THEMES = {"classic", "modern", "bold"}

FIELD_LIMITS = {
    "name": 80,
    "tagline": 120,
    "about": 600,
    "phone": 40,
    "email": 120,
    "address": 600,
    "hours": 300,
    "service_name": 120,
    "service_price_label": 60,
}
