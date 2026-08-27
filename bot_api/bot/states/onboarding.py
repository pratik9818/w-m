from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    """One state: the owner describes their site, the model turns that into a brief.

    The previous ~12 states (name, category, tagline, about, services loop, phone, email,
    address, hours, logo, photos, theme, layout, confirm) each asked for one field. The
    model now infers all of them from a single message, and only asks a follow-up when it
    genuinely cannot tell what the business is.
    """

    waiting_brief = State()
    # What the model read out of the brief is put back to the owner before a site is
    # built from it. A build is the most expensive thing this bot does and it publishes
    # the result, so a misread brief is worth catching while it is still a message.
    waiting_confirm = State()


CATEGORIES = [
    "Restaurant / Cafe",
    "Salon / Spa",
    "Retail Shop",
    "Fitness / Gym",
    "Home Services",
    "Professional Services",
    "Health / Clinic",
    "Education / Tutoring",
    "Other",
]

THEMES = [
    ("classic", "Classic — clean & traditional"),
    ("modern", "Modern — bold & minimal"),
    ("bold", "Bold — vibrant & eye-catching"),
]

MAX_SERVICES = 15
MAX_PHOTOS = 5
