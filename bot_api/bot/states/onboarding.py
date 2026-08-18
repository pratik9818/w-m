from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    waiting_name = State()
    waiting_category = State()
    waiting_category_other = State()
    waiting_tagline = State()
    waiting_about = State()
    waiting_service_name = State()
    waiting_service_price = State()
    waiting_add_another_service = State()
    waiting_phone = State()
    waiting_email = State()
    waiting_address = State()
    waiting_hours = State()
    waiting_logo = State()
    waiting_photos = State()
    waiting_theme = State()
    confirm = State()


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
