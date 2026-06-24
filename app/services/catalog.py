from __future__ import annotations

# Canonical model names and aliases commonly used in listing titles.
# Add aliases gradually after inspecting real titles from the source.
KNOWN_MODELS: dict[str, list[str]] = {
    "BMW": [
        "1 Series",
        "2 Series",
        "3 Series",
        "4 Series",
        "5 Series",
        "6 Series",
        "7 Series",
        "8 Series",
        "X1",
        "X2",
        "X3",
        "X4",
        "X5",
        "X6",
        "X7",
        "XM",
        "Z4",
        "i3",
        "i4",
        "i5",
        "i7",
        "iX",
    ],
    "Audi": ["100", "A3", "A4", "A5", "A6", "A7", "A8", "Q2", "Q3", "Q5", "Q7", "Q8", "TT", "e-tron"],
    "Toyota": [
        "Land Cruiser",
        "Land Cruiser Prado",
        "Camry",
        "Corolla",
        "RAV4",
        "Highlander",
        "Prius",
        "C-HR",
        "Yaris",
    ],
    "Kia": ["Rio", "K5", "K7", "K8", "K9", "Sportage", "Sorento", "Seltos", "Carnival", "Cerato", "Optima"],
}

MODEL_ALIASES: dict[str, dict[str, str]] = {
    "BMW": {
        "1 серия": "1 Series",
        "2 серия": "2 Series",
        "3 серия": "3 Series",
        "4 серия": "4 Series",
        "5 серия": "5 Series",
        "6 серия": "6 Series",
        "7 серия": "7 Series",
        "8 серия": "8 Series",
    },
}


def find_model(brand: str, vehicle_part: str) -> tuple[str | None, str | None]:
    normalized = " ".join(vehicle_part.split())
    if not normalized:
        return None, None

    aliases = MODEL_ALIASES.get(brand, {})
    candidates: list[tuple[str, str]] = [(name, name) for name in KNOWN_MODELS.get(brand, [])]
    candidates.extend((alias, canonical) for alias, canonical in aliases.items())

    for candidate, canonical_name in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        if normalized.lower() == candidate.lower():
            return canonical_name, None
        if normalized.lower().startswith(candidate.lower() + " "):
            return canonical_name, normalized[len(candidate) :].strip() or None

    # Keep unknown models instead of dropping the listing. They can be normalized later.
    parts = normalized.split(maxsplit=1)
    return parts[0], parts[1] if len(parts) == 2 else None
