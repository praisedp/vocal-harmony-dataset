"""Weekly chart locations, in the order of the supplied Spotify dropdown."""

REGIONS = {
    "global": "Global",
    "ar": "Argentina",
    "au": "Australia",
    "at": "Austria",
    "by": "Belarus",
    "be": "Belgium",
    "bo": "Bolivia",
    "br": "Brazil",
    "bg": "Bulgaria",
    "ca": "Canada",
    "cl": "Chile",
    "co": "Colombia",
    "cr": "Costa Rica",
    "cy": "Cyprus",
    "cz": "Czech Republic",
    "dk": "Denmark",
    "do": "Dominican Republic",
    "ec": "Ecuador",
    "eg": "Egypt",
    "sv": "El Salvador",
    "ee": "Estonia",
    "fi": "Finland",
    "fr": "France",
    "de": "Germany",
    "gr": "Greece",
    "gt": "Guatemala",
    "hn": "Honduras",
    "hk": "Hong Kong",
    "hu": "Hungary",
    "is": "Iceland",
    "in": "India",
    "id": "Indonesia",
    "ie": "Ireland",
    "il": "Israel",
    "it": "Italy",
    "jp": "Japan",
    "kz": "Kazakhstan",
    "lv": "Latvia",
    "lt": "Lithuania",
    "lu": "Luxembourg",
    "my": "Malaysia",
    "mx": "Mexico",
    "ma": "Morocco",
    "nl": "Netherlands",
    "nz": "New Zealand",
    "ni": "Nicaragua",
    "ng": "Nigeria",
    "no": "Norway",
    "pk": "Pakistan",
    "pa": "Panama",
    "py": "Paraguay",
    "pe": "Peru",
    "ph": "Philippines",
    "pl": "Poland",
    "pt": "Portugal",
    "ro": "Romania",
    "sa": "Saudi Arabia",
    "sg": "Singapore",
    "sk": "Slovakia",
    "za": "South Africa",
    "kr": "South Korea",
    "es": "Spain",
    "se": "Sweden",
    "ch": "Switzerland",
    "tw": "Taiwan",
    "th": "Thailand",
    "tr": "Turkey",
    "ae": "UAE",
    "ua": "Ukraine",
    "gb": "United Kingdom",
    "uy": "Uruguay",
    "us": "USA",
    "ve": "Venezuela",
    "vn": "Vietnam",
}

GLOBAL_CHART = "regional-global-weekly"
ALL_CHART_TYPES = tuple(f"regional-{code}-weekly" for code in REGIONS)


def resolve_chart_types(selection: str | list[str]) -> tuple[str, ...]:
    """Accept 'all', country codes, or full chart identifiers; Global goes first."""
    if selection == "all" or selection == ["all"]:
        return ALL_CHART_TYPES
    values = [selection] if isinstance(selection, str) else selection
    if not values:
        raise ValueError("Select at least one region or use 'all'.")
    charts = []
    for value in values:
        value = value.strip().lower()
        chart = value if value.startswith("regional-") else f"regional-{value}-weekly"
        if chart not in ALL_CHART_TYPES:
            raise ValueError(f"Unknown weekly chart region: {value!r}. Use --list-regions.")
        if chart not in charts:
            charts.append(chart)
    return tuple(sorted(charts, key=lambda chart: chart != GLOBAL_CHART))


def region_name(chart_type: str) -> str:
    return REGIONS[chart_type.removeprefix("regional-").removesuffix("-weekly")]
