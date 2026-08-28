"""Everything a generated site needs to be findable, written by code rather than a model.

An owner asking for this does not know what SEO is, and should not have to. What they mean
is "when someone searches for a bakery in Leeds, I want to come up" -- and for a small
local business almost all of that is mechanical:

  - a title and description that name the trade and the place
  - a `LocalBusiness` record in the page that search engines read directly, using the
    schema.org type that matches the actual trade -- `Bakery`, `Plumber`, `Dentist`, not a
    generic one
  - a canonical address so two URLs for the same page do not compete with each other
  - the Open Graph tags that decide what a link looks like when it is shared
  - a sitemap and a robots.txt so the pages get crawled at all

None of that is a judgement call, which is why none of it is asked of a model. A model that
writes structured data writes plausible structured data, and a plausible-but-invalid
`LocalBusiness` block is silently ignored by Google -- the worst of both, because it looks
like the job was done. Built here, it is either correct or absent.

What is still the model's job is the words: the title, the description, and the page copy.
Those are in the prompts.

The one hard requirement is knowing the site's own address, which is why it is resolved
before a build rather than after it: a canonical tag pointing nowhere is worse than none.
"""
import html
import json
import re

# Google reads the *most specific* type it is given. "Bakery" tells it what the business
# does; "LocalBusiness" tells it almost nothing. So the owner's own words for their trade
# are matched against schema.org's vocabulary.
#
# The list is ordered most-specific-first -- a "hair salon" is a HairSalon before it is a
# BeautySalon -- but that order is only the tie-break. What decides is where the word falls
# in what the owner wrote, because people lead with what they are: "Restaurant / Cafe" is a
# restaurant and "Bakery and cafe" is a bakery.
#
# https://schema.org/LocalBusiness lists the full set; these are the ones a bot for small
# businesses actually meets.
_SCHEMA_BY_KEYWORD = (
    # food and drink
    (r"\bbakery|baker|patisserie|bread\b", "Bakery"),
    (r"\bcafe|café|coffee|tea ?room|espresso\b", "CafeOrCoffeeShop"),
    (r"\bpub\b|\bbar\b|\btaproom|brewery\b", "BarOrPub"),
    (r"\bice ?cream|gelato\b", "IceCreamShop"),
    (r"\btakeaway|fast ?food|chippy|kebab|burger\b", "FastFoodRestaurant"),
    (r"\brestaurant|bistro|diner|eatery|trattoria|steakhouse|pizzeria\b", "Restaurant"),
    (r"\bcater(?:ing|er)\b", "FoodEstablishment"),
    # hair, beauty, wellbeing
    (r"\bbarber\b", "HairSalon"),
    (r"\bhairdress\w*|\bhair(?:styl\w*)?\b", "HairSalon"),
    (r"\bnail(?:s| bar| salon)\b", "NailSalon"),
    (r"\btattoo|piercing\b", "TattooParlor"),
    (r"\bspa\b|\bmassage|wellness centre|wellness center\b", "DaySpa"),
    (r"\bsalon|beauty|aesthetic|lashes|brows\b", "BeautySalon"),
    # fitness
    (r"\bgym|fitness|crossfit|pilates|yoga|personal train\w*\b", "ExerciseGym"),
    (r"\bsports? (?:club|centre|center)\b", "SportsActivityLocation"),
    # health
    (r"\bdentist|dental|orthodont\w*\b", "Dentist"),
    (r"\bvet(?:erinary|s)?\b", "VeterinaryCare"),
    (r"\bpharmac|chemist\b", "Pharmacy"),
    (r"\boptician|optometr|eyewear\b", "Optician"),
    (r"\bphysio|chiroprac|osteopath|podiatr\w*\b", "MedicalClinic"),
    (r"\bclinic|doctor|gp surgery|medical|health ?care\b", "MedicalClinic"),
    (r"\btherap(?:y|ist)|counsel(?:ling|ing|lor|or)\b", "MedicalClinic"),
    # trades and home services
    (r"\bplumb\w*\b", "Plumber"),
    (r"\belectric(?:ian|al)\b", "Electrician"),
    (r"\broof\w*\b", "RoofingContractor"),
    (r"\bpaint\w*|decorat\w*\b", "HousePainter"),
    (r"\blocksmith\b", "Locksmith"),
    (r"\bremovals?|moving compan\w+|man and van\b", "MovingCompany"),
    (r"\bheating|boiler|hvac|air ?con\w*\b", "HVACBusiness"),
    # schema.org has no cleaning-specific type; the generic one is the honest answer.
    (r"\bclean(?:ing|er)s?\b", "LocalBusiness"),
    (r"\bbuilder|construction|carpent\w*|joiner|landscap\w*|garden\w*|scaffold\w*\b",
     "HomeAndConstructionBusiness"),
    # professional
    (r"\bsolicitor|lawyer|attorney|legal\b", "Attorney"),
    (r"\baccount(?:ant|ing|ancy)\w*|bookkeep\w*|\btax\b", "AccountingService"),
    (r"\binsurance\b", "InsuranceAgency"),
    (r"\bestate agent|realtor|real estate|letting\b", "RealEstateAgent"),
    (r"\bmortgage|financial advis\w*|wealth\b", "FinancialService"),
    (r"\bconsult\w*|agency|marketing|design studio|architect\w*\b", "ProfessionalService"),
    # motoring
    (r"\bgarage|mot\b|\bmechanic|auto ?repair|body ?shop|tyre|tire\b", "AutoRepair"),
    (r"\bcar (?:sales|dealer)|dealership\b", "AutoDealer"),
    (r"\bcar wash|valet\w*\b", "AutoWash"),
    (r"\bdriving (?:school|instructor)|taxi|minicab|chauffeur\b", "LocalBusiness"),
    # learning and children
    (r"\bnursery|childcare|child ?minder|creche|crèche\b", "ChildCare"),
    (r"\bpreschool|pre-school\b", "Preschool"),
    (r"\btutor\w*|school|academy|lessons|training|educat\w*|coaching\b",
     "EducationalOrganization"),
    # shops
    (r"\bflorist|flowers\b", "Florist"),
    (r"\bjewell?er\w*\b", "JewelryStore"),
    (r"\bbutcher|greengrocer|farm ?shop|deli\b", "GroceryStore"),
    (r"\bgrocer|convenience|supermarket|corner shop\b", "GroceryStore"),
    (r"\bhardware|diy|builders? merchant\b", "HardwareStore"),
    (r"\bpet(?: shop| store|s)\b", "PetStore"),
    (r"\bbook ?(?:shop|store)\b", "BookStore"),
    (r"\bfurniture|interiors\b", "FurnitureStore"),
    (r"\bboutique|clothing|fashion|menswear|womenswear|tailor\b", "ClothingStore"),
    (r"\bshop|store|retail\b", "Store"),
    # hospitality and events
    (r"\bhotel|\bb ?& ?b\b|bed and breakfast|guest ?house|inn\b", "LodgingBusiness"),
    (r"\btravel agen\w*|tour\w*\b", "TravelAgency"),
    (r"\bphotograph\w*\b", "Photographer"),
    # "wedding" on its own is a modifier, not a trade: a wedding photographer is a
    # Photographer and a wedding cake baker is a Bakery. Only the phrasings where it
    # names the business itself count.
    (r"\bevents?\b|\bwedding (?:venue|planner|planning)\b|\bdj\b|\bentertain\w*\b",
     "EntertainmentBusiness"),
    (r"\bnight ?club\b", "NightClub"),
    (r"\blaundr\w*|dry clean\w*\b", "DryCleaningOrLaundry"),
    (r"\bstorage\b", "SelfStorage"),
)

# A business with nowhere to visit is not a LocalBusiness, whatever it sells. Claiming to be
# one without an address is the kind of mismatch that gets structured data ignored.
_NOT_LOCAL = re.compile(
    r"\bsoftware|saas|app\b|\bplatform|crypto|token|blockchain|nft|web ?3|"
    r"online (?:shop|store|course)|e-?commerce|newsletter|podcast|blog\b",
    re.IGNORECASE,
)

LOCAL_FALLBACK = "LocalBusiness"
REMOTE_FALLBACK = "Organization"


def schema_type_for(category: str | None, has_address: bool = True) -> str:
    """The schema.org type that best describes this trade.

    Falls back to LocalBusiness for a business with a place customers visit, and to
    Organization for one without -- a crypto project with a `LocalBusiness` record is
    claiming a shopfront it does not have.
    """
    text = (category or "").strip()
    if not has_address and _NOT_LOCAL.search(text):
        return REMOTE_FALLBACK
    # Matched on where the word appears, not on the order of this list. People lead with
    # what they are: "Restaurant / Cafe" is a restaurant and "Bakery and cafe" is a bakery,
    # and a list-ordered match would have called both of them cafes. Ties -- two trades
    # named at the same position, which only happens through alternation inside one
    # pattern -- fall back to list order, which is most-specific-first.
    best = None
    for index, (pattern, schema) in enumerate(_SCHEMA_BY_KEYWORD):
        match = re.search(pattern, text, re.IGNORECASE)
        if match and (best is None or (match.start(), index) < best[0]):
            best = ((match.start(), index), schema)
    if best is not None:
        return best[1]
    return LOCAL_FALLBACK if has_address else REMOTE_FALLBACK


# --------------------------------------------------------------- opening hours

_DAYS = {
    "mon": "Mo", "tue": "Tu", "tues": "Tu", "wed": "We", "thu": "Th", "thur": "Th",
    "thurs": "Th", "fri": "Fr", "sat": "Sa", "sun": "Su",
}
_HOURS_RE = re.compile(
    r"(?P<from>mon|tues?|wed|thur?s?|fri|sat|sun)[a-z]*"
    r"(?:\s*(?:-|to|–|—)\s*(?P<to>mon|tues?|wed|thur?s?|fri|sat|sun)[a-z]*)?"
    r"[\s:,]*"
    r"(?P<open>\d{1,2})(?::(?P<openmin>\d{2}))?\s*(?P<openap>am|pm)?"
    r"\s*(?:-|to|–|—|until|till)\s*"
    r"(?P<close>\d{1,2})(?::(?P<closemin>\d{2}))?\s*(?P<closeap>am|pm)?",
    re.IGNORECASE,
)


def _to_24h(hour: str, minute: str | None, meridiem: str | None) -> str | None:
    value = int(hour)
    if value > 24:
        return None
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and value != 12:
            value += 12
        elif meridiem == "am" and value == 12:
            value = 0
    if value > 24:
        return None
    return f"{value:02d}:{int(minute or 0):02d}"


def opening_hours(display_text: str | None) -> list[str]:
    """The owner's free-text hours, in the format search engines parse. [] when unreadable.

    Owners write hours however they like -- "Mon-Fri 9-6, Sat 10-4", "open 9am til late".
    Google's format is "Mo-Fr 09:00-18:00", and a value it cannot parse is not merely
    ignored: invalid structured data can cost the whole block. So anything this cannot read
    with confidence is left out, and the human-readable hours still appear on the page where
    a person can read them.
    """
    found = []
    for match in _HOURS_RE.finditer(display_text or ""):
        start = _DAYS.get(match.group("from")[:4].lower()) or _DAYS.get(match.group("from")[:3].lower())
        if start is None:
            continue
        end_raw = match.group("to")
        end = None
        if end_raw:
            end = _DAYS.get(end_raw[:4].lower()) or _DAYS.get(end_raw[:3].lower())
        opens = _to_24h(match.group("open"), match.group("openmin"), match.group("openap"))
        closes = _to_24h(match.group("close"), match.group("closemin"), match.group("closeap"))
        if opens is None or closes is None:
            continue
        # "9-6" on a shopfront means 09:00-18:00, never 09:00-06:00. Owners almost never
        # write the pm, and a business that closes before it opens is the giveaway.
        if closes <= opens and match.group("closeap") is None:
            closing = int(closes[:2]) + 12
            if closing <= 23:
                closes = f"{closing:02d}{closes[2:]}"
        if closes <= opens:
            continue
        days = f"{start}-{end}" if end and end != start else start
        found.append(f"{days} {opens}-{closes}")
    return found


# --------------------------------------------------------------- the structured record

def _clean(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def local_business_jsonld(spec: dict, site_url: str | None = None) -> str:
    """The business, as a record search engines read rather than guess at.

    Only what is actually known goes in. An empty or invented field is worse than a missing
    one: this is the machine-readable claim about a real business, and Google cross-checks
    it against what is on the page.
    """
    address = _clean(spec.get("address"))
    schema_type = schema_type_for(spec.get("category"), has_address=bool(address))

    data: dict = {
        "@context": "https://schema.org",
        "@type": schema_type,
        "name": _clean(spec.get("name")) or "Business",
    }
    description = _clean(spec.get("tagline")) or _clean(spec.get("about"))
    if description:
        data["description"] = description[:300]
    if site_url:
        data["url"] = site_url
        # A stable identity for the business across the pages that reference it.
        data["@id"] = f"{site_url.rstrip('/')}/#business"
    if address:
        data["address"] = {"@type": "PostalAddress", "streetAddress": address}
    if _clean(spec.get("phone")):
        data["telephone"] = _clean(spec["phone"])
    if _clean(spec.get("email")):
        data["email"] = _clean(spec["email"])
    if spec.get("logo_url"):
        data["logo"] = spec["logo_url"]
    images = [url for url in (spec.get("photo_urls") or []) if url][:3]
    if images:
        data["image"] = images
    elif spec.get("logo_url"):
        data["image"] = spec["logo_url"]

    hours = opening_hours(spec.get("hours"))
    if hours:
        data["openingHours"] = hours

    services = [
        _clean(service.get("name"))
        for service in (spec.get("services") or [])
        if isinstance(service, dict) and _clean(service.get("name"))
    ]
    if services:
        # What they actually sell, named. For a service business this is the part of the
        # record that matches what someone typed into the search box.
        data["hasOfferCatalog"] = {
            "@type": "OfferCatalog",
            "name": f"{data['name']} services",
            "itemListElement": [
                {"@type": "Offer", "itemOffered": {"@type": "Service", "name": name}}
                for name in services[:20]
            ],
        }

    social = [url for url in (spec.get("social_links") or {}).values() if url]
    if social:
        data["sameAs"] = social

    # `</script>` inside JSON would end the block early; escaping the slash is the standard
    # defence and stays valid JSON.
    encoded = json.dumps(data, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return f'<script type="application/ld+json">\n{encoded}\n</script>'


# --------------------------------------------------------------- the head

def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


# Where the absolute-URL tags are written once the site's address is known. An HTML comment
# is the right thing to leave behind if that never happens: harmless, and visible to anyone
# reading the source, unlike a canonical tag pointing at a placeholder.
URL_MARKER = "<!--seo:url-->"


# Google truncates a search snippet around 160 characters, which is why the prompt asks for
# 140-160. Asked for a range with a hard ceiling, a model writes right up to the ceiling and
# stops -- observed live on xtravu.pages.dev, whose description ended "Trusted by Pizza Hut,
# McDonald" at exactly 160 characters, cut inside the word. A snippet that breaks mid-word
# reads as a broken site in the one place a searcher sees before deciding to click.
#
# So the ceiling is enforced here rather than hoped for in the prompt, and enforced on a
# word boundary: the last whole word that fits wins, and a dangling connective goes with it.
#
# 155 rather than 160 on purpose. 160 is roughly where Google cuts, so a description
# written to exactly 160 is already at the edge -- and a model told "up to 160" writes 160.
# Enforcing a limit below the one the prompt discusses is what makes this guard bite on the
# real failure instead of waving it through at exactly the cap.
DESCRIPTION_MAX_CHARS = 155
# Ending on one of these reads as a sentence cut short even when the word itself is whole.
_DANGLING_WORDS = frozenset({
    "and", "or", "but", "with", "for", "to", "of", "in", "on", "at", "by", "from",
    "the", "a", "an", "as", "if", "so", "that", "than", "then", "plus", "including",
})
_TRAILING_PUNCT = " ,;:-–—/&"


def tidy_description(description: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    """Trim to `limit` without ever cutting a word in half, and never end mid-thought.

    Two separate jobs, and the dangling-word pass runs whether or not anything was trimmed:
    a description already within the limit can still end on "and", which reads as a
    sentence cut short even though every word in it is whole.
    """
    words = " ".join((description or "").split()).split(" ")

    if len(" ".join(words)) > limit:
        kept: list[str] = []
        for word in words:
            candidate = " ".join(kept + [word])
            if len(candidate) > limit:
                break
            kept.append(word)
        words = kept

    while words and words[-1].strip(_TRAILING_PUNCT).lower() in _DANGLING_WORDS:
        words.pop()
    return " ".join(words).rstrip(_TRAILING_PUNCT)


def head_tags(spec: dict, filename: str, title: str, description: str,
              site_url: str | None = None) -> str:
    """The SEO half of <head>: what to index, how to share, and what the business is."""
    name = _clean(spec.get("name")) or "Business"
    tags = [
        # The default is to index, but saying so explicitly is what stops a stray
        # `noindex` from a template or a CDN being the last word.
        '<meta name="robots" content="index, follow, max-image-preview:large">',
        f'<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{_esc(name)}">',
        f'<meta property="og:title" content="{_esc(title)}">',
        f'<meta property="og:description" content="{_esc(description)}">',
        f'<meta property="og:locale" content="en_GB">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{_esc(title)}">',
        f'<meta name="twitter:description" content="{_esc(description)}">',
    ]
    image = next((url for url in (spec.get("photo_urls") or []) if url), None) or spec.get("logo_url")
    if image:
        tags.append(f'<meta property="og:image" content="{_esc(image)}">')
        tags.append(f'<meta name="twitter:image" content="{_esc(image)}">')

    if site_url:
        tags.extend(url_tags(site_url, filename))
    else:
        tags.append(URL_MARKER)

    tags.append(local_business_jsonld(spec, site_url))
    return "\n  ".join(tags)


def url_tags(site_url: str, filename: str) -> list[str]:
    """Canonical and og:url -- the tags that need the site to know its own address.

    Without a canonical, the same page served at "/", "/index.html" and with any tracking
    parameter on the end is three pages competing with each other for the same search.
    """
    base = site_url.rstrip("/")
    # The home page's canonical is the bare domain, not /index.html: that is the address
    # people link to and the one search engines land on.
    page = "" if filename == "index.html" else f"/{filename}"
    absolute = f"{base}{page}" or base
    return [
        f'<link rel="canonical" href="{_esc(absolute)}">',
        f'<meta property="og:url" content="{_esc(absolute)}">',
    ]


# --------------------------------------------------------------- the crawl files

def sitemap_xml(filenames, site_url: str, lastmod: str | None = None) -> str:
    """Every page, listed for crawlers. The home page first and weighted highest."""
    base = site_url.rstrip("/")
    pages = sorted((n for n in filenames if n.endswith(".html")), key=lambda n: n != "index.html")
    entries = []
    for filename in pages:
        loc = base if filename == "index.html" else f"{base}/{filename}"
        entry = [f"  <url>", f"    <loc>{_esc(loc)}</loc>"]
        if lastmod:
            entry.append(f"    <lastmod>{_esc(lastmod)}</lastmod>")
        entry.append(f"    <priority>{'1.0' if filename == 'index.html' else '0.8'}</priority>")
        entry.append("  </url>")
        entries.append("\n".join(entry))
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def robots_txt(site_url: str | None = None) -> str:
    """Crawlers welcome, and told where the map is."""
    lines = ["User-agent: *", "Allow: /"]
    if site_url:
        lines.append(f"Sitemap: {site_url.rstrip('/')}/sitemap.xml")
    return "\n".join(lines) + "\n"


def crawl_files(filenames, site_url: str | None, lastmod: str | None = None) -> dict[str, str]:
    """sitemap.xml and robots.txt, ready to deploy alongside the pages.

    A sitemap needs absolute addresses, so it is only written once the site knows its own.
    robots.txt is worth writing either way -- "everything is crawlable" is the message even
    without a sitemap line.
    """
    files = {"robots.txt": robots_txt(site_url)}
    if site_url:
        files["sitemap.xml"] = sitemap_xml(filenames, site_url, lastmod)
    return files


def finalise_urls(files: dict[str, str], site_url: str, lastmod: str | None = None) -> dict[str, str]:
    """Fill in the address-dependent parts of an already-built site.

    Used when a site is deployed to an address that was not known while it was written --
    the very first build of a brand new project. Every later build has the address from the
    start and leaves no marker to replace.
    """
    finalised = dict(files)
    for filename, content in files.items():
        if filename.endswith(".html") and URL_MARKER in content:
            finalised[filename] = content.replace(
                URL_MARKER, "\n  ".join(url_tags(site_url, filename))
            )
    finalised.update(crawl_files(finalised.keys(), site_url, lastmod))
    return finalised
