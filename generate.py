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


# ── Settings ──────────────────────────────────────────────────────────────────

OUTPUT_ZIP       = Path("gtfs-european-sleeper.zip")
TIMETABLE_PAGE   = "https://www.europeansleeper.eu/en/timetable"

# Internal route IDs used by the ES website, mapped to train numbers
ROUTES = {
    1: "453",
    2: "452",
    5: "475",
    6: "474",
    7: "401",
    8: "400",
}

# Station data from OpenStreetMap (coordinates, ODbL) and Wikidata (UIC codes).
# Keys must match exactly the stop names returned by the ES timetable API.
# Format: name -> (lat, lon, UIC code, IANA timezone)
STOP_DATA = {
    "Prague hl.n. (main station)": (50.0833, 14.4356, "5457076", "Europe/Prague"),
    "Usti nad Labem hl.n.":        (50.6600, 14.0400, "5453179", "Europe/Prague"),
    "Decin hl.n.":                 (50.7742, 14.2136, "5456659", "Europe/Prague"),
    "Bad Schandau":                (50.9156, 14.1517, "8006006", "Europe/Berlin"),
    "Dresden Hbf":                 (51.0407, 13.7322, "8006050", "Europe/Berlin"),
    "Berlin Ostbahnhof":           (52.5103, 13.4343, "8003137", "Europe/Berlin"),
    "Berlin Hauptbahnhof":         (52.5251, 13.3694, "8033452", "Europe/Berlin"),
    "Berlin Gesundbrunnen":        (52.5487, 13.3887, "8007799", "Europe/Berlin"),
    "Berlin-Spandau":              (52.5341, 13.1978, "8003025", "Europe/Berlin"),
    "Arnhem Centraal":             (51.9850,  5.8993, "8400071", "Europe/Amsterdam"),
    "Deventer":                    (52.2558,  6.1580, "8400173", "Europe/Amsterdam"),
    "Amersfoort Centraal":         (52.1531,  5.3789, "8400055", "Europe/Amsterdam"),
    "Amsterdam Bijlmer ArenA":     (52.3126,  4.9469, "8400074", "Europe/Amsterdam"),
    "Amsterdam Centraal":          (52.3791,  4.8997, "8400058", "Europe/Amsterdam"),
    "Utrecht Centraal":            (52.0894,  5.1100, "8400621", "Europe/Amsterdam"),
    "Den Haag HS":                 (52.0697,  4.3242, "8400280", "Europe/Amsterdam"),
    "Rotterdam Centraal":          (51.9248,  4.4689, "8400530", "Europe/Amsterdam"),
    "Breda":                       (51.5956,  4.7797, "8400131", "Europe/Amsterdam"),
    "Roosendaal":                  (51.5292,  4.4636, "8400526", "Europe/Amsterdam"),
    "Antwerpen-Centraal":          (51.2172,  4.4214, "8821006", "Europe/Brussels"),
    "Bruxelles-Midi":              (50.8358,  4.3356, "8814001", "Europe/Brussels"),
    "Hamburg-Harburg":             (53.4566,  9.9922, "8001726", "Europe/Berlin"),
    "Paris Nord":                  (48.8809,  2.3553, "8727103", "Europe/Paris"),
    "Aulnoye-Aymeries":            (50.2028,  3.8408, "8729560", "Europe/Paris"),
    "Mons":                        (50.4542,  3.9517, "8881000", "Europe/Brussels"),
    "Liège-Guillemins":            (50.6242,  5.5664, "8841004", "Europe/Brussels"),
    "Milano Garibaldi":            (45.4847,  9.1883, "8301662", "Europe/Rome"),
    "Como S. Giovanni":            (45.8056,  9.0817, "8301307", "Europe/Rome"),
    "Chiasso":                     (45.8408,  9.0319, "8505307", "Europe/Zurich"),
    "Lugano":                      (46.0044,  8.9475, "8505300", "Europe/Zurich"),
    "Bellinzona":                  (46.1958,  9.0175, "8500122", "Europe/Zurich"),
    "Göschenen":                   (46.6653,  8.5847, "8505119", "Europe/Zurich"),
    "Arth-Goldau":                 (47.0467,  8.5478, "8505004", "Europe/Zurich"),
    "Aarau":                       (47.3917,  8.0506, "8502113", "Europe/Zurich"),
    "Zürich HB":                   (47.3781,  8.5400, "8500010", "Europe/Zurich"),
    "Köln Hbf":                    (50.9428,  6.9586, "8015458", "Europe/Berlin"),
    "Aachen Hbf":                  (50.7678,  6.0908, "8015345", "Europe/Berlin"),
}


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


def fetch_timetable(route_id: int, day: date) -> str:
    """Call the ES timetable API and return the raw HTML response."""
    response = HTTP.post(
        "https://www.europeansleeper.eu/timetable/run",
        data={"departure-date-sql": day.isoformat(), "r": route_id},
        timeout=15,
    )
    return response.text


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
    Fetch every day for every route between start and end.

    Groups days by stop pattern (fingerprint). When the same route has a
    different set of stops on different dates (e.g. Hamburg added in July),
    those become separate variants, each getting their own GTFS trip.
    """
    all_variants = []

    for route_id, train_number in ROUTES.items():
        print(f"ES {train_number}", end="  ", flush=True)

        # Maps stop-name-tuple → { stops, dates[] }
        variants_found = {}

        current_day = start
        day_count = 0

        while current_day <= end:
            html = fetch_timetable(route_id, current_day)

            if html.strip() and "Systeemfout" not in html:
                stops = parse_stops(html)
                if stops:
                    # Use the ordered list of stop names as a unique key
                    pattern = tuple(s["name"] for s in stops)

                    if pattern not in variants_found:
                        variants_found[pattern] = {"stops": stops, "dates": []}

                    variants_found[pattern]["dates"].append(current_day.isoformat())

            day_count += 1
            if day_count % 14 == 0:
                print(".", end="", flush=True)

            current_day += timedelta(days=1)

        total_operating_days = sum(len(v["dates"]) for v in variants_found.values())
        print(f" ✓  {len(variants_found)} variant(s), {total_operating_days} operating days")

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
                "https://www.europeansleeper.eu/en/timetable",
            ])
    return make_csv(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type", "route_url"], rows)


# Even-numbered trains (452/474/400) travel east/south, direction_id 0.
# Odd-numbered trains (453/475/401) travel west/north, direction_id 1.
DIRECTION = {"452": 0, "453": 1, "474": 0, "475": 1, "400": 0, "401": 1}


def build_trips_file(variants: list[dict]) -> str:
    rows = [
        [
            f"ES{v['train']}", v["id"], v["id"], v["destination"], f"ES {v['train']}",
            DIRECTION[v["train"]],
            2,  # wheelchair_accessible: 2 = no accessibility information / not accessible
            2,  # bikes_allowed: 2 = no bikes allowed (suspended as of 2026)
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
            data = STOP_DATA.get(stop["name"])
            if data:
                lat, lon, uic_code, timezone = data
                stops_seen[sid] = (stop["name"], lat, lon, uic_code, timezone)
            else:
                missing.add(stop["name"])

    if missing:
        print(f"⚠  No data for: {', '.join(sorted(missing))}")

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
            ["2", "OpenStreetMap contributors", "1", "0", "0", "https://www.openstreetmap.org"],
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
