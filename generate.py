#!/usr/bin/env python3
"""
European Sleeper GTFS generator.

Fetches live timetable data from the European Sleeper website,
detects all route variants across the season, and writes a GTFS zip.

Usage:
    python3 generate.py
"""

import re
import zipfile
import unicodedata
import requests  # pip install requests
from datetime import date, timedelta
from pathlib import Path
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
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


def query_wikidata_coords_by_uic(uic: str) -> tuple[float, float, str] | None:
    """Look up (lat, lon, country) on Wikidata for a station identified by its exact UIC code."""
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
    return lat, lon, country


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


def fetch_timetable(day: date) -> str:
    """Call the ES timetable API for a single day, across all routes, and return the raw HTML response."""
    response = HTTP.post(
        "https://www.europeansleeper.eu/timetable/run",
        data={"departure-date-sql": day.isoformat(), "r": 0},
        timeout=15,
    )
    return response.text


def split_by_train(html: str) -> dict[str, str]:
    """Split a multi-route /timetable/run response into one HTML chunk per train number."""
    chunks = {}
    matches = list(re.finditer(r'<div[^>]*\bid="(\d+)"', html))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        chunks[match.group(1)] = html[start:end]
    return chunks


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

    Groups days by stop pattern (fingerprint) per train. When the same route
    has a different set of stops on different dates (e.g. Hamburg added in
    July), those become separate variants, each getting their own GTFS trip.
    """
    # train_number → { stop-name-tuple → { stops, dates[] } }
    variants_by_train = {train: {} for train in TRAIN_NUMBERS}
    train_numbers = set(TRAIN_NUMBERS)

    current_day = start
    day_count = 0
    total_days = (end - start).days + 1

    while current_day <= end:
        html = fetch_timetable(current_day)

        if html.strip() and "Systeemfout" not in html:
            chunks = split_by_train(html)

            for train_number, chunk in chunks.items():
                if train_number not in train_numbers:
                    continue
                stops = parse_stops(chunk)
                if not stops:
                    continue

                pattern = tuple(s["name"] for s in stops)
                variants_found = variants_by_train[train_number]
                if pattern not in variants_found:
                    variants_found[pattern] = {"stops": stops, "dates": []}
                variants_found[pattern]["dates"].append(current_day.isoformat())

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


def to_gtfs_time(hhmm: str, extra_minutes: int) -> str:
    """
    Convert "HH:MM" to a GTFS time string, adding extra_minutes for overnight offsets.

    GTFS allows times past 24:00 for trips that cross midnight, e.g. "29:09:00"
    means 05:09 the next day. extra_minutes is a multiple of 24*60 incremented
    each time the train crosses midnight.
    """
    hours, minutes = map(int, hhmm.split(":"))
    total_minutes = hours * 60 + minutes + extra_minutes
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}:00"



def build_stop_times(stops: list[dict]) -> list[tuple[str, str]]:
    """
    Return a (arrival, departure) GTFS time pair for each stop.
    Detects midnight crossings by watching for times that go backwards.
    """
    result = []
    previous_departure_minutes = -1
    overnight_offset = 0  # increases by 24*60 each time the train crosses midnight

    for stop in stops:
        reference_time = stop["arrival"] or stop["departure"] or "00:00"
        ref_hours, ref_mins = map(int, reference_time.split(":"))
        current_minutes = ref_hours * 60 + ref_mins

        if previous_departure_minutes >= 0 and current_minutes < previous_departure_minutes - 30:
            overnight_offset += 24 * 60

        last_time = stop["departure"] or stop["arrival"] or "00:00"
        last_h, last_m = map(int, last_time.split(":"))
        previous_departure_minutes = last_h * 60 + last_m

        arrival   = to_gtfs_time(stop["arrival"],   overnight_offset) if stop["arrival"]   else None
        departure = to_gtfs_time(stop["departure"], overnight_offset) if stop["departure"] else None

        # First stop: no arrival, use departure. Last stop: no departure, use arrival.
        result.append((arrival or departure, departure or arrival))

    return result


def make_csv(headers: list[str], rows: list[list]) -> str:
    """Build a CSV string with quoted fields."""
    def quote(value):
        return '"' + str(value).replace('"', '""') + '"'

    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(quote(cell) for cell in row))

    return "\n".join(lines) + "\n"


def build_agency_file() -> str:
    return make_csv(
        ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang", "agency_fare_url"],
        [["ES", "European Sleeper", "https://www.europeansleeper.eu", "Europe/Brussels", "en",
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


def fetch_bicycle_reservation_windows() -> list[tuple[date, date]]:
    """Fetch the date ranges ES currently accepts bicycles for, from their booking API."""
    response = HTTP.get(ES_CONSTANTS_API, timeout=15)
    response.raise_for_status()
    windows = response.json()["settings"]["bicycleReservationDates"]
    return [(date.fromisoformat(w["start"]), date.fromisoformat(w["end"])) for w in windows]


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
    rows = []
    for v in variants:
        times = build_stop_times(v["stops"])
        for sequence, (stop, (arrival, departure)) in enumerate(zip(v["stops"], times), start=1):
            sid = make_stop_id(stop["name"])
            rows.append([
                v["id"], arrival, departure, sid, sequence,
                0,  # pickup_type: 0 = regular scheduled pickup
                0,  # drop_off_type: 0 = regular scheduled drop-off
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
