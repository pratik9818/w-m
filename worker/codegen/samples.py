"""Hand-written sample specs for standalone codegen testing.

Same dict shape as builder.spec_from_business() output, covering categories/themes
not yet exercised by real Telegram test data.
"""

SAMPLES: dict[str, dict] = {
    "plumber": {
        "name": "Rivera Plumbing",
        "category": "Plumber",
        "tagline": "Fast, honest plumbing repairs",
        "about": (
            "Rivera Plumbing has served the east side for over 15 years. We handle "
            "everything from leaky faucets to full pipe replacements, with upfront "
            "pricing and same-day appointments for most jobs."
        ),
        "theme": "modern",
        "phone": "+1-555-201-4477",
        "email": "contact@riveraplumbing.example",
        "address": "482 Elm Street, Riverside",
        "hours": "Mon-Fri 7am-6pm, Sat 8am-2pm, closed Sunday",
        "services": [
            {"name": "Drain cleaning", "price_label": "from $89"},
            {"name": "Water heater repair", "price_label": "from $150"},
            {"name": "Pipe replacement", "price_label": "Contact for quote"},
            {"name": "Emergency leak repair", "price_label": "from $120"},
        ],
        "logo_url": None,
        "photo_urls": [],
    },
    "salon": {
        "name": "Bloom Hair Studio",
        "category": "Hair Salon",
        "tagline": "Where your best look begins",
        "about": (
            "Bloom Hair Studio is a boutique salon specializing in color, cuts, and "
            "styling for every hair type. Our stylists trained in NYC and LA before "
            "bringing their craft home."
        ),
        "theme": "bold",
        "phone": "+1-555-330-9981",
        "email": "hello@bloomhairstudio.example",
        "address": "12 Market Square, Suite 4",
        "hours": "Tue-Sat 9am-7pm, closed Sun-Mon",
        "services": [
            {"name": "Women's cut & style", "price_label": "$65"},
            {"name": "Men's cut", "price_label": "$35"},
            {"name": "Full color", "price_label": "from $120"},
            {"name": "Balayage", "price_label": "from $180"},
            {"name": "Blowout", "price_label": "$45"},
        ],
        "logo_url": None,
        "photo_urls": [],
    },
}
