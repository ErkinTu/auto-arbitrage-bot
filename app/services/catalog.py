from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class CatalogModelSeed:
    canonical_name: str
    aliases: tuple[str, ...] = ()


# This is only the bootstrap catalog. The database aliases are the source of truth at runtime.
# Unknown models are deliberately not added here automatically.
DEFAULT_CATALOG: dict[str, tuple[CatalogModelSeed, ...]] = {
    "BMW": (
        CatalogModelSeed("1 Series", ("1 серия", "1 серии", "1 series")),
        CatalogModelSeed("2 Series", ("2 серия", "2 серии", "2 series")),
        CatalogModelSeed("3 Series", ("3 серия", "3 серии", "3 series")),
        CatalogModelSeed("4 Series", ("4 серия", "4 серии", "4 series")),
        CatalogModelSeed("5 Series", ("5 серия", "5 серии", "5 series")),
        CatalogModelSeed("6 Series", ("6 серия", "6 серии", "6 series")),
        CatalogModelSeed("7 Series", ("7 серия", "7 серии", "7 series")),
        CatalogModelSeed("8 Series", ("8 серия", "8 серии", "8 series")),
        CatalogModelSeed("X1"),
        CatalogModelSeed("X2"),
        CatalogModelSeed("X3"),
        CatalogModelSeed("X4"),
        CatalogModelSeed("X5"),
        CatalogModelSeed("X6"),
        CatalogModelSeed("X7"),
        CatalogModelSeed("XM"),
        CatalogModelSeed("Z4"),
        CatalogModelSeed("M2"),
        CatalogModelSeed("M3"),
        CatalogModelSeed("M4"),
        CatalogModelSeed("M5"),
        CatalogModelSeed("M8"),
        CatalogModelSeed("i3"),
        CatalogModelSeed("i4"),
        CatalogModelSeed("i5"),
        CatalogModelSeed("i7"),
        CatalogModelSeed("iX"),
    ),
    "Audi": (
        CatalogModelSeed("80"),
        CatalogModelSeed("100"),
        CatalogModelSeed("A3"),
        CatalogModelSeed("A4"),
        CatalogModelSeed("A5"),
        CatalogModelSeed("A6"),
        CatalogModelSeed("A7"),
        CatalogModelSeed("A8"),
        CatalogModelSeed("S3"),
        CatalogModelSeed("S4"),
        CatalogModelSeed("S5"),
        CatalogModelSeed("S6"),
        CatalogModelSeed("S7"),
        CatalogModelSeed("S8"),
        CatalogModelSeed("RS3"),
        CatalogModelSeed("RS4"),
        CatalogModelSeed("RS5"),
        CatalogModelSeed("RS6"),
        CatalogModelSeed("RS7"),
        CatalogModelSeed("Q2"),
        CatalogModelSeed("Q2L"),
        CatalogModelSeed("Q3"),
        CatalogModelSeed("Q5"),
        CatalogModelSeed("Q6"),
        CatalogModelSeed("Q7"),
        CatalogModelSeed("Q8"),
        CatalogModelSeed("TT"),
        CatalogModelSeed("e-tron", ("e tron", "etron")),
    ),
    "Toyota": (
        CatalogModelSeed("4Runner", ("4 Runner",)),
        CatalogModelSeed("Alphard"),
        CatalogModelSeed("Aristo"),
        CatalogModelSeed("Avensis"),
        CatalogModelSeed("Aygo X", ("Aygo-X",)),
        CatalogModelSeed("Camry"),
        CatalogModelSeed("C-HR", ("CHR", "C HR")),
        CatalogModelSeed("Corolla"),
        CatalogModelSeed("Granvia"),
        CatalogModelSeed("Harrier"),
        CatalogModelSeed("Highlander"),
        CatalogModelSeed("Ipsum"),
        CatalogModelSeed("Land Cruiser Prado", ("LC Prado",)),
        CatalogModelSeed("Land Cruiser"),
        CatalogModelSeed("Mark II", ("Mark 2", "Mark II", "Mark Ⅱ")),
        CatalogModelSeed("Prius"),
        CatalogModelSeed("RAV4", ("RAV 4", "RAV-4")),
        CatalogModelSeed("Wish"),
        CatalogModelSeed("Yaris"),
    ),
    "Kia": (
        CatalogModelSeed("Rio"),
        CatalogModelSeed("K5"),
        CatalogModelSeed("K7"),
        CatalogModelSeed("K8"),
        CatalogModelSeed("K9"),
        CatalogModelSeed("Sportage"),
        CatalogModelSeed("Sorento"),
        CatalogModelSeed("Seltos"),
        CatalogModelSeed("Carnival"),
        CatalogModelSeed("Cerato"),
        CatalogModelSeed("Optima"),
    ),
}


@dataclass(frozen=True, slots=True)
class TitleVehicleParts:
    source_model_text: str | None
    source_generation_text: str | None
    suggested_model: str | None
    suggested_generation: str | None


def normalize_alias_text(value: str) -> str:
    """Normalise user/source spellings for deterministic alias lookups."""
    compact = " ".join(value.strip().split())
    compact = compact.replace("ё", "е").replace("Ё", "Е")
    compact = compact.replace("–", "-").replace("—", "-")
    return compact.casefold()


def is_bootstrap_model(brand: str | None, model: str | None) -> bool:
    if not brand or not model:
        return False
    target = normalize_alias_text(model)
    return any(
        normalize_alias_text(seed.canonical_name) == target
        for seed in DEFAULT_CATALOG.get(brand, ())
    )


def _to_roman(value: int) -> str:
    if not 1 <= value <= 39:
        return str(value)
    pairs = ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    result: list[str] = []
    remaining = value
    for number, token in pairs:
        while remaining >= number:
            result.append(token)
            remaining -= number
    return "".join(result)


_YEAR_TOKEN = r"(?:19|20)\d{2}"
_N_V_TOKEN = r"н\s*\.?\s*в\s*\.?"
_PERIOD_RE = re.compile(
    rf"(?<!\d){_YEAR_TOKEN}\s*[-–—]\s*(?:{_YEAR_TOKEN}|{_N_V_TOKEN})(?!\d)",
    flags=re.IGNORECASE,
)
_ROMAN_RE = re.compile(r"^[IVXLCDM]{1,8}$", flags=re.IGNORECASE)
_ROMAN_OR_CODE_RE = r"(?:[IVXLCDM]{1,8}|[A-Z]{1,4}\d{1,4}(?:/[A-Z0-9]{1,4})*)"
_FACELIFT_RE = re.compile(r"р(?:е|э)(?:й)?стайлинг\b", flags=re.IGNORECASE)


def _normalise_generation_surface(value: str) -> str:
    clean = " ".join(value.strip(" -:|,").split())
    clean = clean.replace("–", "-").replace("—", "-")
    # Mashina titles use several spellings: н.в., н. в., нв. Keep one canonical spelling.
    clean = re.sub(_N_V_TOKEN, "н.в.", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*-\s*", "-", clean)
    return clean


def _strip_wrapping_generation_parentheses(value: str) -> str:
    """Remove parentheses only when they wrap the whole generation descriptor."""
    match = re.fullmatch(r"\(\s*(.*?)\s*\)", value)
    if not match:
        return value
    inner = match.group(1).strip()
    if not inner:
        return value
    if _ROMAN_RE.fullmatch(inner) or _PERIOD_RE.fullmatch(inner) or re.fullmatch(_ROMAN_OR_CODE_RE, inner, re.I):
        return inner
    return value


def _normalise_roman_tokens(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return token.upper() if _ROMAN_RE.fullmatch(token) else token

    return re.sub(r"(?<![A-Za-z0-9])([IVXLCDM]{1,8})(?![A-Za-z0-9])", replace, value, flags=re.IGNORECASE)


def canonicalize_generation(value: str | None) -> str | None:
    """Canonicalise Mashina generation notation while retaining its real meaning.

    Accepted examples:
    - ``IV (G30/G31) Рестайлинг``
    - ``Рейстайлинг (V)`` -> ``V Рестайлинг``
    - ``2016 - 2026`` -> ``2016-2026``
    - ``(2016-н.в.)`` -> ``2016-н.в.``
    - ``4 поколения`` -> ``IV``
    """
    if not value:
        return None
    clean = _normalise_generation_surface(value)
    if not clean:
        return None

    # Numeric Russian forms are a generation designation, not a listing production year.
    number_match = re.match(r"^(\d{1,2})\s*(?:поколени[ея]|gen\.?)(.*)$", clean, flags=re.IGNORECASE)
    if number_match:
        number = _to_roman(int(number_match.group(1)))
        tail = number_match.group(2).strip(" -:|,")
        clean = f"{number} {tail}".strip()

    clean = _strip_wrapping_generation_parentheses(clean)

    # Normalize the common typo "Рейстайлинг" and place facelift after the actual generation.
    facelift_match = _FACELIFT_RE.search(clean)
    if facelift_match:
        remainder = (clean[: facelift_match.start()] + " " + clean[facelift_match.end() :]).strip(" -:|,")
        remainder = _strip_wrapping_generation_parentheses(_normalise_generation_surface(remainder))
        clean = f"{remainder} Рестайлинг".strip() if remainder else "Рестайлинг"

    clean = _normalise_roman_tokens(clean)
    return clean or None


def split_listing_year_suffix(vehicle_part: str) -> tuple[str, int | None]:
    """Separate a final listing year without consuming a generation production period.

    ``2016-2026`` and ``2016-н.в.`` belong to the generation descriptor. A final standalone
    year after such a period remains the actual vehicle year.
    """
    clean = " ".join(vehicle_part.strip().split())
    if not clean:
        return clean, None

    protected_spans = [match.span() for match in _PERIOD_RE.finditer(clean)]
    candidates = [
        match
        for match in re.finditer(rf"(?<!\d)({_YEAR_TOKEN})(?!\d)", clean)
        if not any(start <= match.start() and match.end() <= end for start, end in protected_spans)
    ]
    if not candidates:
        return clean, None

    candidate = candidates[-1]
    trailing = clean[candidate.end() :].strip(" .,:;|-")
    if trailing:
        return clean, None

    before = clean[: candidate.start()].rstrip(" .,:;|-")
    return before, int(candidate.group(1))


def _split_unknown_model_and_generation(vehicle_part: str) -> tuple[str | None, str | None]:
    """Conservative fallback when a model is not in the bootstrap catalog yet."""
    clean = " ".join(vehicle_part.strip().split())
    if not clean:
        return None, None

    # Sources use Roman numerals, codes such as G05, production periods and standalone facelift text.
    generation_start = re.search(
        rf"\s+(?=(?:\(\s*(?:{_ROMAN_OR_CODE_RE}|{_PERIOD_RE.pattern})\s*\)|"
        rf"{_ROMAN_OR_CODE_RE}(?=\s|\(|$)|{_YEAR_TOKEN}\s*[-–—]\s*(?:{_YEAR_TOKEN}|{_N_V_TOKEN})|"
        rf"р(?:е|э)(?:й)?стайлинг\b))",
        clean,
        flags=re.IGNORECASE,
    )
    if generation_start:
        model = clean[: generation_start.start()].strip()
        generation = clean[generation_start.end() :].strip()
        return model or None, generation or None

    return clean, None


def parse_vehicle_part(brand: str, vehicle_part: str) -> TitleVehicleParts:
    """Extract raw model/generation text without ever auto-accepting an unknown model."""
    normalized = " ".join(vehicle_part.strip().split())
    if not normalized:
        return TitleVehicleParts(None, None, None, None)

    candidates: list[tuple[str, str]] = []
    for seed in DEFAULT_CATALOG.get(brand, ()):
        candidates.append((seed.canonical_name, seed.canonical_name))
        candidates.extend((alias, seed.canonical_name) for alias in seed.aliases)

    for candidate, canonical_model in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"^{re.escape(candidate)}(?:\s+|$)", flags=re.IGNORECASE)
        match = pattern.match(normalized)
        if not match:
            continue
        source_model = normalized[: match.end()].strip()
        remainder = normalized[match.end() :].strip(" -:|,")
        return TitleVehicleParts(
            source_model_text=source_model,
            source_generation_text=remainder or None,
            suggested_model=canonical_model,
            suggested_generation=canonicalize_generation(remainder),
        )

    source_model, source_generation = _split_unknown_model_and_generation(normalized)
    return TitleVehicleParts(
        source_model_text=source_model,
        source_generation_text=source_generation,
        suggested_model=None,
        suggested_generation=canonicalize_generation(source_generation),
    )
