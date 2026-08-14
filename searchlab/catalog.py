"""A product catalogue in real English, for the half of search that meaning drives.

The synthetic profiles generate text by drawing words at random, which is the
right shape for measuring how an engine moves bytes: field cardinality, term
distribution, index size. It is the wrong corpus for asking whether a search
*found the right thing*, because "ember rank desert cipher boost" is not about
anything. Nothing is relevant to it, so relevance cannot be scored, and a
vector search over it can be fast and correct-looking while meaning nothing.

This module generates documents that are about something. Each one is built
from a category — running shoes, espresso machines — so the words in it belong
together, and so the category is known ground truth: a query for "comfortable
shoes for jogging" should return running shoes, and any it misses is a real
miss rather than an artefact of nonsense data.

Fields match the default profile, so a catalogue index is a drop-in for the
generated one: same names, same types, same dashboard.
"""

from __future__ import annotations

import random
from typing import Any

# Each category carries the words that genuinely co-occur with it. The
# `sells_as` phrases are how a shopper would describe wanting one, and they
# are deliberately *not* copied into the documents — they are the query side
# of the ground truth, and a query that shares no words with its answer is
# exactly the case lexical search fails and vector search should not.
CATEGORIES: list[dict[str, Any]] = [
    {
        "slug": "running-shoes", "dept": "Footwear", "noun": "running shoes",
        "materials": ["breathable mesh", "engineered knit", "recycled polyester"],
        "features": ["a cushioned midsole", "a carbon plate", "reflective trim",
                     "a wide toe box", "responsive foam"],
        "benefits": ["long training runs", "race day", "daily mileage",
                     "road and treadmill use"],
        "sells_as": ["comfortable shoes for jogging",
                     "what should I wear for a marathon",
                     "lightweight trainers for long distances"],
        "price": (60, 240),
    },
    {
        "slug": "hiking-boots", "dept": "Footwear", "noun": "hiking boots",
        "materials": ["full-grain leather", "waterproof suede", "ripstop nylon"],
        "features": ["a lugged outsole", "ankle support", "a waterproof membrane",
                     "a rock plate"],
        "benefits": ["wet trails", "multi-day treks", "loose scree",
                     "cold weather walking"],
        "sells_as": ["footwear for walking in the mountains",
                     "waterproof boots for wet trails",
                     "what to wear hiking in the rain"],
        "price": (80, 320),
    },
    {
        "slug": "espresso-machines", "dept": "Kitchen", "noun": "espresso machine",
        "materials": ["brushed stainless steel", "matte aluminium", "powder-coated steel"],
        "features": ["a PID temperature controller", "a 58mm portafilter",
                     "a steam wand", "a pre-infusion cycle", "a dual boiler"],
        "benefits": ["pulling consistent shots", "milk drinks at home",
                     "small kitchens", "morning routines"],
        "sells_as": ["how do I make good coffee at home",
                     "machine for making lattes",
                     "something for a proper morning brew"],
        "price": (150, 2400),
    },
    {
        "slug": "office-chairs", "dept": "Furniture", "noun": "office chair",
        "materials": ["woven mesh", "bonded leather", "moulded foam"],
        "features": ["adjustable lumbar support", "a synchro-tilt mechanism",
                     "4D armrests", "a seat depth slider"],
        "benefits": ["long days at a desk", "lower back pain", "tall users",
                     "shared workspaces"],
        "sells_as": ["my back hurts when I work",
                     "something to sit on all day",
                     "chair for a home office"],
        "price": (120, 1400),
    },
    {
        "slug": "laptops", "dept": "Electronics", "noun": "laptop",
        "materials": ["a machined aluminium chassis", "a magnesium alloy body",
                      "a carbon-fibre lid"],
        "features": ["a 14-hour battery", "a high-refresh display",
                     "a backlit keyboard", "32 GB of memory", "a fanless design"],
        "benefits": ["travel", "video editing", "software development",
                     "lecture halls"],
        "sells_as": ["portable computer for writing code",
                     "something light to carry when travelling",
                     "machine for editing video on the move"],
        "price": (400, 3500),
    },
    {
        "slug": "headphones", "dept": "Electronics", "noun": "over-ear headphones",
        "materials": ["memory-foam earcups", "a folding steel headband",
                      "vegan leather padding"],
        "features": ["active noise cancellation", "40-hour battery life",
                     "a transparency mode", "multipoint pairing"],
        "benefits": ["open-plan offices", "long flights", "commuting",
                     "concentration"],
        "sells_as": ["how do I block out noise in an office",
                     "something quiet for a long flight",
                     "headphones for concentrating"],
        "price": (40, 600),
    },
    {
        "slug": "tents", "dept": "Outdoor", "noun": "backpacking tent",
        "materials": ["silnylon", "ripstop polyester", "an aluminium pole set"],
        "features": ["a full-coverage rainfly", "a bathtub floor",
                     "two vestibules", "a sub-2kg packed weight"],
        "benefits": ["exposed pitches", "shoulder-season trips",
                     "carrying it all day", "wet ground"],
        "sells_as": ["shelter for sleeping outdoors",
                     "something light to carry for camping",
                     "what do I sleep in on a long walk"],
        "price": (90, 800),
    },
    {
        "slug": "cast-iron-cookware", "dept": "Kitchen", "noun": "cast iron skillet",
        "materials": ["seasoned cast iron", "enamelled cast iron"],
        "features": ["a helper handle", "a pour spout", "an oven-safe body",
                     "a pre-seasoned surface"],
        "benefits": ["searing steak", "oven-to-table cooking", "induction hobs",
                     "campfires"],
        "sells_as": ["pan that gets really hot for searing",
                     "cookware that lasts a lifetime",
                     "what should I fry a steak in"],
        "price": (25, 220),
    },
    {
        "slug": "winter-jackets", "dept": "Apparel", "noun": "insulated jacket",
        "materials": ["800-fill down", "synthetic insulation", "a recycled shell"],
        "features": ["a helmet-compatible hood", "pit zips", "a DWR finish",
                     "an elasticated storm cuff"],
        "benefits": ["sub-zero mornings", "wind off the sea", "layering",
                     "standing around in the cold"],
        "sells_as": ["how do I stay warm in freezing weather",
                     "coat for a very cold winter",
                     "something warm that packs down small"],
        "price": (70, 700),
    },
    {
        "slug": "mechanical-keyboards", "dept": "Electronics", "noun": "mechanical keyboard",
        "materials": ["a CNC aluminium case", "PBT keycaps", "a gasket mount"],
        "features": ["hot-swap switches", "a south-facing layout",
                     "QMK firmware", "a rotary encoder"],
        "benefits": ["long typing sessions", "quiet offices", "programmers",
                     "small desks"],
        "sells_as": ["keyboard that feels good to type on all day",
                     "something quiet for typing in a shared room",
                     "best thing to write code on"],
        "price": (60, 400),
    },
    {
        "slug": "road-bikes", "dept": "Cycling", "noun": "road bike",
        "materials": ["a carbon frame", "a butted aluminium frame", "a titanium frame"],
        "features": ["hydraulic disc brakes", "electronic shifting",
                     "internal cable routing", "tubeless-ready wheels"],
        "benefits": ["long climbs", "commuting quickly", "group rides",
                     "rough tarmac"],
        "sells_as": ["fast bicycle for riding on the road",
                     "something quick for getting to work",
                     "bike for climbing hills"],
        "price": (600, 9000),
    },
    {
        "slug": "monitors", "dept": "Electronics", "noun": "monitor",
        "materials": ["an IPS panel", "an OLED panel", "a matte coating"],
        "features": ["a 27-inch 4K display", "a USB-C dock", "a height-adjustable stand",
                     "factory colour calibration"],
        "benefits": ["reading text for hours", "colour work", "two-machine desks",
                     "small rooms"],
        "sells_as": ["big screen for working at a desk",
                     "display that is easy on the eyes",
                     "screen for editing photos accurately"],
        "price": (150, 1800),
    },
]

BRANDS = ["Kestrel", "Northvale", "Aldergrove", "Fenwick", "Marlowe", "Ardent",
          "Halcyon", "Brightwater", "Stonefield", "Copperline", "Wren & Co",
          "Ravenna", "Thornbury", "Silvercreek", "Aubrey Works"]

_QUALIFIERS = ["lightweight", "durable", "compact", "premium", "everyday",
               "professional", "entry-level", "heavy-duty"]
_SERIES = ["Mk II", "Pro", "Classic", "Lite", "XT", "Studio", "Trail", "Signature"]


def _sentence(rng: random.Random, cat: dict) -> str:
    """One claim about the product, in the vocabulary of its own category."""
    shape = rng.randrange(4)
    feature = rng.choice(cat["features"])
    benefit = rng.choice(cat["benefits"])
    material = rng.choice(cat["materials"])
    if shape == 0:
        return f"Built with {material} and {feature}."
    if shape == 1:
        return f"{feature.capitalize()} makes it suited to {benefit}."
    if shape == 2:
        return f"Designed for {benefit}, with {feature}."
    return f"The {material} construction holds up to {benefit}."


def product_doc(seq: int, rng: random.Random) -> dict[str, Any]:
    """One coherent document: every field agrees with the category."""
    cat = rng.choice(CATEGORIES)
    brand = rng.choice(BRANDS)
    qualifier = rng.choice(_QUALIFIERS)
    series = rng.choice(_SERIES)
    material = rng.choice(cat["materials"])

    title = f"{brand} {series} {qualifier} {cat['noun']}"
    body = " ".join(_sentence(rng, cat) for _ in range(rng.randint(2, 4)))

    lo, hi = cat["price"]
    # Price tracks the category rather than the whole catalogue, so a filter
    # on price selects a plausible slice instead of a random one.
    price = round(rng.uniform(lo, hi), 2)

    tags = rng.sample(
        [cat["slug"], cat["dept"].lower(), qualifier,
         material.split()[-1], *[f.split()[-1] for f in cat["features"]]],
        k=rng.randint(2, 5))

    return {
        "id": f"doc-{seq}",
        "title_t": title,
        "body_t": f"{title}. {body}",
        "category_s": cat["slug"],
        "department_s": cat["dept"],
        "brand_s": brand,
        "price_f": price,
        "stock_i": rng.randint(0, 500),
        "active_b": rng.random() < 0.95,
        "tags_ss": tags,
    }


def benchmark_queries() -> list[dict[str, Any]]:
    """Natural-language queries paired with the categories that answer them.

    Relevance is judged at the category level: every running shoe answers
    "comfortable shoes for jogging" and nothing else does. That is coarse
    next to human judgements, but it is real ground truth rather than a
    guess, and it is derived from how the documents were built rather than
    from a model's opinion about them.
    """
    out = []
    for cat in CATEGORIES:
        for phrase in cat["sells_as"]:
            out.append({"query": phrase, "relevant": [cat["slug"]]})
    return out
