"""Bengaluru market reference data: localities, aliases, neighbours and indicative prices.

Prices are rough 2025-26 resale averages (INR per sq ft) used only to generate a
realistic synthetic inventory; swap in your own listings for real use.
"""

LOCALITIES = {
    # name: (price_per_sqft, zone)
    "Whitefield": (8200, "east"),
    "Marathahalli": (8400, "east"),
    "Bellandur": (9800, "east"),
    "Sarjapur Road": (7600, "southeast"),
    "HSR Layout": (12500, "southeast"),
    "Koramangala": (15500, "central"),
    "Indiranagar": (17000, "central"),
    "Jayanagar": (15000, "south"),
    "JP Nagar": (10500, "south"),
    "Banashankari": (9800, "south"),
    "Kanakapura Road": (7200, "south"),
    "Electronic City": (6000, "southeast"),
    "Hebbal": (11000, "north"),
    "Yelahanka": (7000, "north"),
    "Hennur": (7800, "north"),
    "Thanisandra": (7400, "north"),
}

# Hand-curated neighbour pairs: a buyer asking for one is often happy with the other.
NEIGHBOURS = {
    "Whitefield": {"Marathahalli", "Bellandur"},
    "Marathahalli": {"Whitefield", "Bellandur"},
    "Bellandur": {"Marathahalli", "Sarjapur Road", "HSR Layout", "Whitefield"},
    "Sarjapur Road": {"Bellandur", "HSR Layout", "Electronic City"},
    "HSR Layout": {"Koramangala", "Bellandur", "Sarjapur Road"},
    "Koramangala": {"HSR Layout", "Indiranagar", "Jayanagar"},
    "Indiranagar": {"Koramangala"},
    "Jayanagar": {"JP Nagar", "Banashankari", "Koramangala"},
    "JP Nagar": {"Jayanagar", "Banashankari", "Kanakapura Road"},
    "Banashankari": {"Jayanagar", "JP Nagar", "Kanakapura Road"},
    "Kanakapura Road": {"JP Nagar", "Banashankari"},
    "Electronic City": {"Sarjapur Road"},
    "Hebbal": {"Yelahanka", "Hennur", "Thanisandra"},
    "Yelahanka": {"Hebbal"},
    "Hennur": {"Hebbal", "Thanisandra"},
    "Thanisandra": {"Hennur", "Hebbal"},
}

# Spellings people actually type in chats and portal forms.
ALIASES = {
    "whitefield": "Whitefield", "white field": "Whitefield", "itpl": "Whitefield",
    "marathahalli": "Marathahalli", "marathalli": "Marathahalli",
    "bellandur": "Bellandur",
    "sarjapur": "Sarjapur Road", "sarjapur road": "Sarjapur Road", "sarjapura": "Sarjapur Road",
    "hsr": "HSR Layout", "hsr layout": "HSR Layout",
    "koramangala": "Koramangala", "kormangala": "Koramangala",
    "indiranagar": "Indiranagar", "indira nagar": "Indiranagar",
    "jayanagar": "Jayanagar",
    "jp nagar": "JP Nagar", "j p nagar": "JP Nagar", "jpnagar": "JP Nagar",
    "banashankari": "Banashankari", "bsk": "Banashankari",
    "kanakapura": "Kanakapura Road", "kanakapura road": "Kanakapura Road",
    "electronic city": "Electronic City", "ecity": "Electronic City", "e city": "Electronic City",
    "hebbal": "Hebbal",
    "yelahanka": "Yelahanka",
    "hennur": "Hennur",
    "thanisandra": "Thanisandra",
}

BHK_SIZES = {1: 650, 2: 1150, 3: 1600, 4: 2400}


def canonical_locality(text: str):
    key = text.strip().lower()
    if key in ALIASES:
        return ALIASES[key]
    for name in LOCALITIES:
        if name.lower() == key:
            return name
    return None


def location_affinity(wanted: list, locality: str) -> float:
    """1.0 exact match, 0.6 neighbouring locality, 0 otherwise (0.5 if buyer gave no locality)."""
    if not wanted:
        return 0.5
    if locality in wanted:
        return 1.0
    if any(locality in NEIGHBOURS.get(w, set()) for w in wanted):
        return 0.6
    return 0.0
