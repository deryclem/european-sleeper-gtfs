#!/usr/bin/env python3
"""
European Sleeper GTFS generator.

Fetches live timetable data from the European Sleeper website,
detects all route variants across the season, and writes a GTFS zip.

Usage:
    python3 generate.py
"""

import re
import time
import zipfile
import unicodedata
import requests  # pip install requests
from datetime import date, datetime, timedelta, timezone
from functools import cache
from zoneinfo import ZoneInfo
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
HTTP = requests.Session()
HTTP.headers.update({
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.europeansleeper.eu/en/timetable",
})
HTTP.mount("https://", HTTPAdapter(max_retries=_retry))

# Separate session for Wikidata: it requires an identifiable User-Agent
# (https://w.wiki/4wJS) and shouldn't carry the ES-specific headers above.
WIKIDATA_HTTP = requests.Session()
WIKIDATA_HTTP.headers.update({
    "User-Agent": "european-sleeper-gtfs/1.0 (https://github.com/deryclem/european-sleeper-gtfs)",
})
WIKIDATA_HTTP.mount("https://", HTTPAdapter(max_retries=_retry))


# ── Settings ──────────────────────────────────────────────────────────────────

OUTPUT_ZIP       = Path("gtfs-european-sleeper.zip")
TIMETABLE_PAGE   = "https://www.europeansleeper.eu/en/timetable"

TRAIN_NUMBERS = ["453", "452", "475", "474", "401", "400"]

ES_CONSTANTS_API = "https://europeansleeperprod-api.azurewebsites.net/api/constants"

# All ES stations are on Central European Time, so GTFS times use one zone.
AGENCY_TIMEZONE = ZoneInfo("Europe/Brussels")

# ES's brand color (bg-dark-aubergine on booking.europeansleeper.eu).
ROUTE_COLOR = "40002C"
ROUTE_TEXT_COLOR = "FFFFFF"

# ── Station lookup (European Sleeper official data + Wikidata) ────────────────
#
# UIC codes for regularly-served stations come from ES's own booking API
# (europeansleeperprod-api.azurewebsites.net/api/constants), which spells
# some names differently than the timetable HTML (e.g. "Rotterdam Centraal"
# vs "Rotterdam CS") — hence the explicit mapping below rather than a name
# match. Coordinates aren't in that API, so Wikidata still supplies lat/lon
# for every stop, and is the sole source (UIC included) for stops missing
# here — detours and seasonal stops like Verviers or Amsterdam Bijlmer ArenA.
STOP_NAME_TO_ES_UIC = {
    "Bruxelles-Midi":               "8814001",
    "Antwerpen-Centraal":           "8821006",
    "Roosendaal":                   "8400526",
    "Rotterdam Centraal":           "8400530",
    "Amersfoort Centraal":          "8400055",
    "Deventer":                     "8400173",
    "Dresden Hbf":                  "8006050",
    "Bad Schandau":                 "8006006",
    "Decin hl.n.":                  "5455659",
    "Usti nad Labem hl.n.":         "5453179",
    "Prague hl.n. (main station)":  "5457076",
    "Den Haag HS":                  "8400280",
    "Amsterdam Centraal":           "8400058",
    "Berlin Hauptbahnhof":          "8065969",
    "Berlin Ostbahnhof":            "8003004",
    "Aulnoye-Aymeries":             "8729560",
    "Mons":                         "8881000",
    "Liège-Guillemins":             "8841004",
    "Hamburg-Harburg":              "8001134",
    "Paris Nord":                   "8727100",
    "Aachen Hbf":                   "8015345",
    "Köln Hbf":                     "8015458",
    "Arth-Goldau":                  "8505004",
    "Göschenen":                    "8505119",
    "Bellinzona":                   "8505213",
    "Lugano":                       "8505300",
    "Como S. Giovanni":             "8301307",
    "Milano Garibaldi":             "8301645",
    "Breda":                        "8400131",
    "Eindhoven Centraal":           "8400206",
    "St-Quentin":                   "8729600",
    "Verviers":                     "8844008",
}

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

# Tried in order until one yields a match; covers the languages of the
# countries ES timetables run through.
SEARCH_LANGUAGES = ["en", "fr", "de", "nl", "it", "cs"]

COUNTRY_TIMEZONE = {
    "Czech Republic":     "Europe/Prague",
    "Germany":            "Europe/Berlin",
    "Netherlands":        "Europe/Amsterdam",
    "Belgium":            "Europe/Brussels",
    "France":             "Europe/Paris",
    "Switzerland":         "Europe/Zurich",
    "Italy":              "Europe/Rome",
}


def clean_station_name(name: str) -> str:
    """Strip qualifiers that hurt Wikidata's search but don't identify the station."""
    name = re.sub(r"\(.*?\)", "", name)          # "(main station)"
    name = re.sub(r"\bhl\.?\s*n\.?\b\.?", "", name, flags=re.IGNORECASE)  # Czech "hl.n."
    name = re.sub(r"\bS\.\s*", "San ", name)      # Italian "S." abbreviation
    name = re.sub(r"\bSt[-.]\s*", "Saint-", name, flags=re.IGNORECASE)   # French "St-" / "St." abbreviation
    return re.sub(r"\s+", " ", name).strip()


def query_wikidata_stations(search_term: str, language: str) -> list[dict]:
    """Search Wikidata for railway stations matching search_term, best match first."""
    query = f"""
    SELECT ?item ?itemLabel ?uic ?coord ?countryLabel ?rank WHERE {{
      SERVICE wikibase:mwapi {{
        bd:serviceParam wikibase:api "EntitySearch".
        bd:serviceParam wikibase:endpoint "www.wikidata.org".
        bd:serviceParam mwapi:search "{search_term}".
        bd:serviceParam mwapi:language "{language}".
        ?item wikibase:apiOutputItem mwapi:item.
        ?rank wikibase:apiOrdinal true.
      }}
      ?item wdt:P31/wdt:P279* wd:Q55488.  # instance of (a subclass of) railway station
      ?item wdt:P722 ?uic.
      ?item wdt:P625 ?coord.
      ?item wdt:P17 ?country.
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    ORDER BY ?rank
    """
    response = WIKIDATA_HTTP.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["results"]["bindings"]


_uic_coords_cache: dict[str, tuple[float, float, str]] = {}


def prefetch_wikidata_uic_coords(uics: list[str]) -> None:
    """Batch-fetch coordinates and country for UIC codes in a single Wikidata SPARQL query."""
    if not uics:
        return
    uic_str = " ".join(f'"{u}"' for u in set(uics) if u)
    query = f"""
    SELECT ?uic ?coord ?countryLabel WHERE {{
      VALUES ?uic {{ {uic_str} }}
      ?item wdt:P722 ?uic.
      ?item wdt:P625 ?coord.
      OPTIONAL {{ ?item wdt:P17 ?country. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    """
    try:
        response = WIKIDATA_HTTP.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=20,
        )
        response.raise_for_status()
        for b in response.json()["results"]["bindings"]:
            u = b["uic"]["value"]
            lon, lat = map(float, b["coord"]["value"][6:-1].split())
            country = b.get("countryLabel", {}).get("value")
            _uic_coords_cache[u] = (lat, lon, country)
    except Exception as e:
        print(f"⚠ Batch UIC prefetch failed ({e}), falling back to individual queries")


def query_wikidata_coords_by_uic(uic: str) -> tuple[float, float, str] | None:
    """Look up (lat, lon, country) on Wikidata for a station identified by its exact UIC code."""
    if uic in _uic_coords_cache:
        return _uic_coords_cache[uic]

    query = f"""
    SELECT ?coord ?countryLabel WHERE {{
      ?item wdt:P722 "{uic}".
      ?item wdt:P625 ?coord.
      OPTIONAL {{ ?item wdt:P17 ?country. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    LIMIT 1
    """
    response = WIKIDATA_HTTP.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=20,
    )
    response.raise_for_status()
    bindings = response.json()["results"]["bindings"]
    if not bindings:
        return None
    lon, lat = map(float, bindings[0]["coord"]["value"][6:-1].split())
    country = bindings[0].get("countryLabel", {}).get("value")
    result = (lat, lon, country)
    _uic_coords_cache[uic] = result
    return result


_station_cache: dict[str, dict] = {}


def resolve_station(name: str) -> dict:
    """
    Resolve a stop name to its coordinates, UIC code and timezone.

    Returns {"lat", "lon", "uic", "timezone"}, or None if nothing matched.
    """
    if name in _station_cache:
        return _station_cache[name]

    es_uic = STOP_NAME_TO_ES_UIC.get(name)
    if es_uic:
        coords = query_wikidata_coords_by_uic(es_uic)
        if coords:
            lat, lon, country = coords
            result = {"lat": lat, "lon": lon, "uic": es_uic, "timezone": COUNTRY_TIMEZONE.get(country)}
            _station_cache[name] = result
            return result

    cleaned = clean_station_name(name)
    bindings = []
    for language in SEARCH_LANGUAGES:
        bindings = query_wikidata_stations(cleaned, language)
        if bindings:
            break

    if not bindings:
        _station_cache[name] = None
        return None

    # A single station can list more than one UIC code; keep the smallest
    # deterministically. Distinct stations (different Wikidata items) are
    # a real ambiguity worth flagging.
    by_item = {}
    for b in bindings:
        item = b["item"]["value"]
        by_item.setdefault(item, []).append(b)

    if len(by_item) > 1:
        candidates = ", ".join(
            f"{group[0]['itemLabel']['value']} (UIC {min(g['uic']['value'] for g in group)})"
            for group in by_item.values()
        )
        print(f"⚠  Ambiguous station match for {name!r}: {candidates} — using the first")

    best_group = next(iter(by_item.values()))
    lon, lat = map(float, best_group[0]["coord"]["value"][6:-1].split())
    country = best_group[0]["countryLabel"]["value"]
    timezone = COUNTRY_TIMEZONE.get(country)
    uic = es_uic or min(g["uic"]["value"] for g in best_group)

    result = {"lat": lat, "lon": lon, "uic": uic, "timezone": timezone}
    _station_cache[name] = result
    return result


# ── Fetching data from the ES website ─────────────────────────────────────────

def fetch_date_range() -> tuple[date, date]:
    """
    Read the timetable page to find the scan start and end dates.

    The page embeds a JS datepicker with:
        minDate: 0  (today)
        maxDate = new Date(YYYY, MM - 1, DD)

    We use today as start and the maxDate value as end.
    """
    response = HTTP.get(TIMETABLE_PAGE, timeout=15)
    match = re.search(r'maxDate\s*=\s*new Date\((\d+),\s*(\d+)\s*-\s*1,\s*(\d+)\)', response.text)
    if not match:
        raise RuntimeError("Could not find maxDate on the timetable page. The page structure may have changed.")
    year, month, day_ = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return date.today(), date(year, month, day_)


SERVER_ERROR_RETRY_DELAYS = [10, 30, 90]  # seconds

# If ES keeps failing on a day, the feed stops the day before, unless that
# leaves less than this many days of timetable.
MIN_FEED_DAYS = 30


class TimetableServerError(RuntimeError):
    pass


def fetch_timetable(day: date) -> str:
    """
    Call the ES timetable API for a single day, across all routes, and return the raw HTML response.

    ES answers server errors with a 200 "Systeemfout" page. Those are retried,
    then raised: skipping the day would silently drop its trains from the feed.
    """
    for delay in [*SERVER_ERROR_RETRY_DELAYS, None]:
        response = HTTP.post(
            "https://www.europeansleeper.eu/timetable/run",
            data={"departure-date-sql": day.isoformat(), "r": 0},
            timeout=15,
        )
        if "Systeemfout" not in response.text:
            return response.text
        if delay is None:
            raise TimetableServerError(f"ES timetable returned a server error for {day} after {len(SERVER_ERROR_RETRY_DELAYS)} retries.")
        print(f"\n⚠  ES server error for {day}, retrying in {delay}s")
        time.sleep(delay)


def split_by_train(html: str) -> list[tuple[str, str]]:
    """Split a multi-route /timetable/run response into (train number, HTML chunk) pairs."""
    chunks = []
    matches = list(re.finditer(r'<div[^>]*\bid="(\d+)"', html))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunks.append((match.group(1), html[start:end]))
    return chunks


def parse_departure_date(html: str) -> date | None:
    """
    Extract the actual departure date from a train's header table.

    The response for a given day also lists trains that left the day before
    and arrive that day, so the requested date isn't the departure date:
        <td>Wed 23 September 2026</td>   ← departure
        <td>Thu 24 September 2026</td>   ← arrival
    """
    match = re.search(r'<td>\s*\w+ (\d{1,2} \w+ \d{4})\s*</td>', html)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%d %B %Y").date()


def parse_stops(html: str) -> list[dict]:
    """
    Extract stop names and times from the timetable HTML.

    Each stop block looks like:
        <div class="flex margin-top">
            <b>19:22</b>
            <span class="flex-col stop">
                Bruxelles-Midi
                <i>Arrival 19:20</i>   ← only present for intermediate stops
            </span>
        </div>

    The first stop has no arrival time.
    The last stop has no departure time (the <b> time is its arrival).
    """
    stops = []

    for block in re.finditer(r'<div class="flex margin-top">(.*?)</div>', html, re.DOTALL):
        content = block.group(1)

        time_match    = re.search(r'<b>\s*([\d:]+)\s*</b>', content)
        name_match    = re.search(r'class="flex-col stop[^"]*">\s*\n?\s*([^\n<]+)', content)
        arrival_match = re.search(r'Arrival\s*([\d:]+)', content)

        if not time_match or not name_match:
            continue

        stops.append({
            "name":      name_match.group(1).strip(),
            "departure": time_match.group(1).strip(),
            "arrival":   arrival_match.group(1).strip() if arrival_match else None,
        })

    if stops:
        stops[0]["arrival"]    = None
        stops[-1]["arrival"]   = stops[-1]["departure"]  # the bold time on the last stop is arrival
        stops[-1]["departure"] = None

    return stops


# ── Scanning the full season ───────────────────────────────────────────────────

def scan_season(start: date, end: date) -> list[dict]:
    """
    Fetch every day between start and end, one request covering all routes at once.

    Each run is dated by the departure date in its header, not the requested
    day, and is kept once even though it shows up in two consecutive responses.

    Groups runs by stop pattern (fingerprint) per train. When the same route
    has different stops or times on different dates (e.g. Hamburg added in
    July), those become separate variants, each getting their own GTFS trip.
    """
    # train_number → { (name, arrival, departure)-tuple → { stops, dates[] } }
    variants_by_train = {train: {} for train in TRAIN_NUMBERS}
    train_numbers = set(TRAIN_NUMBERS)
    seen_runs = set()  # (train_number, departure date)

    current_day = start
    day_count = 0
    total_days = (end - start).days + 1

    while current_day <= end:
        try:
            html = fetch_timetable(current_day)
        except TimetableServerError as error:
            # Every train departing before this day was already listed, so
            # stopping here leaves a shorter but complete feed.
            if (current_day - start).days < MIN_FEED_DAYS:
                raise RuntimeError(f"{error} Not writing a feed shorter than {MIN_FEED_DAYS} days.") from error
            print(f"\n⚠  {error} The feed will end on {current_day - timedelta(days=1)}.")
            total_days = (current_day - start).days
            break

        if html.strip():
            chunks = split_by_train(html)

            for train_number, chunk in chunks:
                if train_number not in train_numbers:
                    continue
                departure_date = parse_departure_date(chunk)
                if departure_date is None:
                    raise RuntimeError(f"No departure date for ES {train_number} on {current_day}. The page structure may have changed.")
                if (train_number, departure_date) in seen_runs:
                    continue
                stops = parse_stops(chunk)
                if not stops:
                    continue
                seen_runs.add((train_number, departure_date))

                pattern = tuple((s["name"], s["arrival"], s["departure"]) for s in stops)
                variants_found = variants_by_train[train_number]
                if pattern not in variants_found:
                    variants_found[pattern] = {"stops": stops, "dates": []}
                variants_found[pattern]["dates"].append(departure_date.isoformat())

        day_count += 1
        if day_count % 14 == 0:
            print(".", end="", flush=True)

        current_day += timedelta(days=1)

    print(f" ✓  scanned {total_days} days\n")

    all_variants = []
    for train_number, variants_found in variants_by_train.items():
        total_operating_days = sum(len(v["dates"]) for v in variants_found.values())
        print(f"ES {train_number}  {len(variants_found)} variant(s), {total_operating_days} operating days")

        for i, variant in enumerate(variants_found.values(), start=1):
            variant_id = f"ES{train_number}_v{i}"
            stops = variant["stops"]
            dates = sorted(variant["dates"])

            all_variants.append({
                "id":          variant_id,
                "train":       train_number,
                "origin":      stops[0]["name"],
                "destination": stops[-1]["name"],
                "stops":       stops,
                "dates":       dates,
            })

            stop_names = " → ".join(s["name"] for s in stops)
            print(f"    {variant_id:12}  {dates[0]} → {dates[-1]}  ({len(dates)} days)")
            print(f"    {'':12}  {stop_names}")

    return all_variants


# ── Building GTFS files ────────────────────────────────────────────────────────

def make_stop_id(name: str) -> str:
    """Convert a station name to a simple ASCII identifier, e.g. 'Liege-Guillemins' -> 'liege_guillemins'."""
    # Strip accents first, then replace anything non-alphanumeric with underscores
    without_accents = "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "_", without_accents.lower()).strip("_")


def to_gtfs_time(service_day: date, hhmm: str, days_later: int) -> str:
    """
    Convert a local "HH:MM" clock time to a GTFS time for the given service day.

    GTFS times count the time elapsed since "noon minus 12h" on the service day,
    so "29:09:00" usually means 05:09 the next day. On the nights clocks change,
    that same 05:09 becomes "30:09:00" in October and "28:09:00" in March.
    """
    hours, minutes = map(int, hhmm.split(":"))
    clock_day = service_day + timedelta(days=days_later)
    local = datetime(clock_day.year, clock_day.month, clock_day.day, hours, minutes, tzinfo=AGENCY_TIMEZONE)
    noon = datetime(service_day.year, service_day.month, service_day.day, 12, tzinfo=AGENCY_TIMEZONE)
    # Subtract in UTC: Python ignores offsets when both datetimes share a tzinfo.
    elapsed = local.astimezone(timezone.utc) - (noon.astimezone(timezone.utc) - timedelta(hours=12))
    total_minutes = int(elapsed.total_seconds()) // 60
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}:00"


def build_stop_times(stops: list[dict], service_day: date) -> list[tuple[str, str]]:
    """
    Return a (arrival, departure) GTFS time pair for each stop.
    Detects midnight crossings by watching for times that go backwards.
    """
    result = []
    previous_departure_minutes = -1
    days_later = 0  # increases by 1 each time the train crosses midnight

    for stop in stops:
        reference_time = stop["arrival"] or stop["departure"] or "00:00"
        ref_hours, ref_mins = map(int, reference_time.split(":"))
        current_minutes = ref_hours * 60 + ref_mins

        if previous_departure_minutes >= 0 and current_minutes < previous_departure_minutes - 30:
            days_later += 1

        last_time = stop["departure"] or stop["arrival"] or "00:00"
        last_h, last_m = map(int, last_time.split(":"))
        previous_departure_minutes = last_h * 60 + last_m

        arrival   = to_gtfs_time(service_day, stop["arrival"],   days_later) if stop["arrival"]   else None
        departure = to_gtfs_time(service_day, stop["departure"], days_later) if stop["departure"] else None

        # First stop: no arrival, use departure. Last stop: no departure, use arrival.
        result.append((arrival or departure, departure or arrival))

    return result


def split_by_clock_change(variants: list[dict]) -> list[dict]:
    """
    Attach GTFS times to each variant, splitting off the dates where they differ.

    A variant shares one set of stop_times across all its dates, but a night
    train running when the clocks change gets times shifted by an hour, so
    those dates become a separate trip (e.g. ES453_v3 and ES453_v3_2).
    """
    result = []
    for v in variants:
        dates_by_times = {}
        for day in v["dates"]:
            times = tuple(build_stop_times(v["stops"], date.fromisoformat(day)))
            dates_by_times.setdefault(times, []).append(day)
        for i, (times, dates) in enumerate(dates_by_times.items(), start=1):
            variant_id = v["id"] if i == 1 else f"{v['id']}_{i}"
            result.append({**v, "id": variant_id, "dates": dates, "times": list(times)})
    return result


def make_csv(headers: list[str], rows: list[list]) -> str:
    """Build a CSV string with quoted fields."""
    def quote(value):
        if value is None:
            return '""'
        return '"' + str(value).replace('"', '""') + '"'

    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(quote(cell) for cell in row))

    return "\n".join(lines) + "\n"


def build_agency_file() -> str:
    return make_csv(
        ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang", "agency_fare_url"],
        [["ES", "European Sleeper", "https://www.europeansleeper.eu", AGENCY_TIMEZONE.key, "en",
          "https://booking.europeansleeper.eu/en"]],
    )


def build_routes_file(variants: list[dict]) -> str:
    seen = set()
    rows = []
    for v in variants:
        route_id = f"ES{v['train']}"
        if route_id not in seen:
            seen.add(route_id)
            rows.append([
                route_id, "ES", f"ES {v['train']}", f"{v['origin']} → {v['destination']}", 2,
                "https://www.europeansleeper.eu/en/timetable", ROUTE_COLOR, ROUTE_TEXT_COLOR,
            ])
    return make_csv(
        ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "route_url",
         "route_color", "route_text_color"],
        rows,
    )


# Even-numbered trains (452/474/400) travel east/south, direction_id 0.
# Odd-numbered trains (453/475/401) travel west/north, direction_id 1.
DIRECTION = {"452": 0, "453": 1, "474": 0, "475": 1, "400": 0, "401": 1}


@cache
def fetch_es_constants() -> dict:
    """Fetch ES's booking settings (bicycle windows, sales rules…) once per run."""
    response = HTTP.get(ES_CONSTANTS_API, timeout=15)
    response.raise_for_status()
    return response.json()


def fetch_bicycle_reservation_windows() -> list[tuple[date, date]]:
    """Fetch the date ranges ES currently accepts bicycles for, from their booking API."""
    windows = fetch_es_constants()["settings"]["bicycleReservationDates"]
    return [(date.fromisoformat(w["start"]), date.fromisoformat(w["end"])) for w in windows]


def fetch_domestic_journey_disabled_countries() -> set[str]:
    """
    Fetch the countries where ES doesn't sell domestic trips (e.g. Amsterdam → Deventer).

    ES identifies countries by their UIC country code, the first two digits of
    a station's UIC code: "84" for the Netherlands, "88" for Belgium…
    """
    return set(fetch_es_constants()["domesticJourneyDisabledCountries"])


def boarding_rules(stops: list[dict], restricted_countries: set[str]) -> list[tuple[int, int]]:
    """
    Return a (pickup_type, drop_off_type) pair for each stop, 1 meaning not allowed.

    GTFS can only restrict a stop, not a pair of stops, so the domestic-trip
    rule is applied to the first and last countries of the trip: no drop-off
    where the train starts, no pickup where it ends. A domestic trip within a
    country the train only passes through stays possible in the feed.
    """
    countries = [resolve_station(s["name"])["uic"][:2] for s in stops]
    first_country, last_country = countries[0], countries[-1]
    leading = next((i for i, c in enumerate(countries) if c != first_country), len(countries))
    trailing = len(countries) - next(
        (i for i, c in enumerate(reversed(countries)) if c != last_country), len(countries)
    )
    single_country = leading == len(countries)

    rules = []
    for i in range(len(stops)):
        no_drop_off = i == 0 or (not single_country and i < leading and first_country in restricted_countries)
        no_pickup = i == len(stops) - 1 or (not single_country and i >= trailing and last_country in restricted_countries)
        rules.append((1 if no_pickup else 0, 1 if no_drop_off else 0))
    return rules


def trip_allows_bikes(dates: list[str], windows: list[tuple[date, date]]) -> bool:
    """A trip allows bikes if any of its operating dates falls in a bicycle reservation window."""
    trip_dates = [date.fromisoformat(d) for d in dates]
    return any(start <= d <= end for d in trip_dates for start, end in windows)


def build_trips_file(variants: list[dict]) -> str:
    bike_windows = fetch_bicycle_reservation_windows()
    rows = [
        [
            f"ES{v['train']}", v["id"], v["id"], v["destination"], f"ES {v['train']}",
            DIRECTION[v["train"]],
            2,  # wheelchair_accessible: 2 = not accessible (ES: trains do not meet accessibility requirements)
            1 if trip_allows_bikes(v["dates"], bike_windows) else 2,
        ]
        for v in variants
    ]
    return make_csv(
        ["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name",
         "direction_id", "wheelchair_accessible", "bikes_allowed"],
        rows,
    )


def build_calendar_dates_file(variants: list[dict]) -> str:
    rows = []
    for v in variants:
        for day in v["dates"]:
            rows.append([v["id"], day.replace("-", ""), 1])
    return make_csv(["service_id", "date", "exception_type"], rows)


def build_stops_file(variants: list[dict]) -> str:
    prefetch_wikidata_uic_coords(list(STOP_NAME_TO_ES_UIC.values()))
    stops_seen = {}  # stop_id → (name, lat, lon, uic_code, timezone)
    missing = set()

    for v in variants:
        for stop in v["stops"]:
            sid = make_stop_id(stop["name"])
            if sid in stops_seen:
                continue
            data = resolve_station(stop["name"])
            if data:
                stops_seen[sid] = (stop["name"], data["lat"], data["lon"], data["uic"], data["timezone"])
            else:
                missing.add(stop["name"])

    if missing:
        raise RuntimeError(f"No Wikidata match for stations: {', '.join(sorted(missing))}")

    rows = [
        [sid, name, lat, lon, uic_code, timezone]
        for sid, (name, lat, lon, uic_code, timezone) in stops_seen.items()
    ]
    return make_csv(["stop_id", "stop_name", "stop_lat", "stop_lon", "stop_code", "stop_timezone"], rows)


def build_stop_times_file(variants: list[dict]) -> str:
    restricted_countries = fetch_domestic_journey_disabled_countries()
    rows = []
    for v in variants:
        rules = boarding_rules(v["stops"], restricted_countries)
        for sequence, (stop, (arrival, departure), (pickup, drop_off)) in enumerate(
            zip(v["stops"], v["times"], rules), start=1
        ):
            sid = make_stop_id(stop["name"])
            rows.append([
                v["id"], arrival, departure, sid, sequence,
                pickup,    # pickup_type: 0 = regular, 1 = no pickup
                drop_off,  # drop_off_type: 0 = regular, 1 = no drop-off
                1,  # timepoint: 1 = exact scheduled times (not estimates)
            ])
    return make_csv(
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence",
         "pickup_type", "drop_off_type", "timepoint"],
        rows,
    )


def build_feed_info_file(variants: list[dict]) -> str:
    all_dates = sorted(day for v in variants for day in v["dates"])
    return make_csv(
        ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version", "feed_contact_url"],
        [["european-sleeper-gtfs", "https://github.com/deryclem/european-sleeper-gtfs", "en",
          all_dates[0].replace("-", ""), all_dates[-1].replace("-", ""),
          date.today().strftime("%Y%m%d"), "https://github.com/deryclem/european-sleeper-gtfs/issues"]],
    )


def build_attributions_file() -> str:
    return make_csv(
        ["attribution_id", "organization_name", "is_producer", "is_operator", "is_authority", "attribution_url"],
        [
            ["1", "European Sleeper", "0", "1", "0", "https://www.europeansleeper.eu"],
            ["2", "Wikidata contributors", "1", "0", "0", "https://www.wikidata.org"],
        ],
    )


def build_gtfs(variants: list[dict]) -> dict[str, str]:
    variants = split_by_clock_change(variants)
    return {
        "agency.txt":         build_agency_file(),
        "stops.txt":          build_stops_file(variants),
        "routes.txt":         build_routes_file(variants),
        "calendar_dates.txt": build_calendar_dates_file(variants),
        "trips.txt":          build_trips_file(variants),
        "stop_times.txt":     build_stop_times_file(variants),
        "feed_info.txt":      build_feed_info_file(variants),
        "attributions.txt":   build_attributions_file(),
    }


def write_zip(files: dict[str, str], path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, content in files.items():
            z.writestr(filename, content)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching date range from European Sleeper…")
    scan_start, scan_end = fetch_date_range()
    print(f"Scanning {scan_start} → {scan_end}  (~2 min)\n")
    variants = scan_season(scan_start, scan_end)
    if not variants:
        raise RuntimeError("No variants found. Possible network error or rate limit, not writing an empty feed.")

    print("\nBuilding GTFS…")
    files = build_gtfs(variants)

    for filename, content in files.items():
        record_count = content.count("\n") - 1
        print(f"  {filename:22}  {record_count} records")

    write_zip(files, OUTPUT_ZIP)
    print(f"\n✓ Written to {OUTPUT_ZIP}")


if __name__ == "__main__":
    main()
