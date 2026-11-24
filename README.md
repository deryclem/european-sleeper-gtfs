# gtfs-european-sleeper

Unofficial [GTFS](https://gtfs.org) feed for European Sleeper night trains, built from data European Sleeper doesn't publish as GTFS.

## Why this exists

European Sleeper only publishes a timetable page meant for humans. This project scrapes it and rebuilds the feed: which trains run on which days, which stops they make, and where those stops are.

It is also used by [Panto](https://getpanto.app).

## Download

[gtfs-european-sleeper.zip](./gtfs-european-sleeper.zip)

## Trains

The feed covers the trains listed in `TRAIN_NUMBERS` in `generate.py` (452, 453, 474, 475, 400, 401). A new train number has to be added there by hand.

When a train's stops or times change during the season, each pattern becomes its own trip variant, tied to the dates it runs. A night train running when the clocks change gets a separate trip for those dates (suffix `_2`).

## Where the data comes from

| Source | What it provides |
|--------|-------------------|
| `europeansleeper.eu/timetable/run` | The endpoint behind their timetable page. Not documented. Returns per-day HTML for every route in one call, which this scrapes for stop names and times. |
| `europeansleeperprod-api.azurewebsites.net/api/constants` | The booking backend's config endpoint. Gives UIC codes for the stations listed in `STOP_NAME_TO_ES_UIC`, the bicycle reservation periods, and the countries where domestic trips aren't sold. |
| [Wikidata](https://www.wikidata.org) | Coordinates and timezone for every stop, plus UIC codes for stops missing from the booking API (detours, seasonal stations). |

## Generating

Requires Python 3 and `requests`.

```bash
pip install -r requirements.txt
python3 generate.py
```

Scans from today to the last date offered by the timetable page, one request per day, then resolves each station. A GitHub Actions workflow runs it every Monday at 04:00 UTC (or on manual trigger) and commits the zip.

## Data rules

- Each run is dated by its departure date. The timetable endpoint returns a train under the previous day when it departs after midnight, so runs are deduplicated.
- ES sometimes answers with a "Systeemfout" page. The request is retried 3 times (10 s, 30 s, 90 s). If it keeps failing, the feed stops the day before. If that leaves fewer than 30 days, nothing is written.
- If `query.wikidata.org` is down or slow, generation fails and no zip is written.
- `pickup_type` and `drop_off_type`: no pickup at the last stop and no drop-off at the first. Domestic trips are also blocked at the ends of the route in countries where ES doesn't sell them (from the `constants` endpoint).
- `bikes_allowed` is set when one of the trip's dates falls in a bicycle reservation period.
- `wheelchair_accessible` is 2 (not accessible), as stated by ES.

## Limitations

- No fares: European Sleeper's pricing is dynamic and this feed is only rebuilt weekly, so baked-in prices would be stale.
- No shapes: there is no route geometry source that doesn't involve map-matching against OSM.
- Times are the planned ones from the timetable page. Delays, cancellations and short-notice changes aren't reflected.
- Station names come from whatever European Sleeper's site returns that day. Ambiguous matches against Wikidata are logged rather than silently guessed.
- Some rules above work around limits of the source data. If the source is fixed, they have no effect.

## License

Feed: [CC0](https://creativecommons.org/publicdomain/zero/1.0/). Station data: © Wikidata contributors, CC0.

Not affiliated with European Sleeper Exploitatie B.V. or the European Sleeper Coöperatie U.A.
