from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot_api.bot.states.onboarding import CATEGORIES, THEMES


def category_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in CATEGORIES:
        builder.button(text=category, callback_data=f"category:{category}")
    builder.adjust(2)
    return builder.as_markup()


def theme_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, label in THEMES:
        builder.button(text=label, callback_data=f"theme:{key}")
    builder.adjust(1)
    return builder.as_markup()


def photo_placement_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏷 Use as my logo", callback_data="photo:logo")
    builder.button(text="🖼 Big picture at the top", callback_data="photo:hero")
    builder.button(text="📷 Add to my photo gallery", callback_data="photo:gallery")
    builder.button(text="🙋 Next to my About text", callback_data="photo:about")
    builder.button(text="✖ Never mind", callback_data="photo:cancel")
    builder.adjust(1)
    return builder.as_markup()


def layout_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 One landing page", callback_data="layout:landing")
    builder.button(text="📚 Four pages (Home, About, Services, Contact)", callback_data="layout:multipage")
    builder.adjust(1)
    return builder.as_markup()


def add_another_service_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Add another service", callback_data="service:add_another")
    builder.button(text="✅ Done adding services", callback_data="service:done")
    builder.adjust(1)
    return builder.as_markup()


def confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Create my site", callback_data="onboarding:confirm")
    builder.button(text="🔄 Start over", callback_data="onboarding:restart")
    builder.adjust(1)
    return builder.as_markup()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 My Sites", callback_data="menu:mysites")
    builder.button(text="➕ Create New Site", callback_data="menu:newsite")
    builder.button(text="❓ Help", callback_data="menu:help")
    builder.adjust(1)
    return builder.as_markup()


def sites_list_keyboard(businesses: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for business in businesses:
        builder.button(text=f"✏️ {business.name}", callback_data=f"site:select:{business.id}")
    builder.button(text="➕ Create New Site", callback_data="menu:newsite")
    builder.adjust(1)
    return builder.as_markup()
