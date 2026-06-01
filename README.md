# gtfs-european-sleeper

[GTFS](https://gtfs.org) feed for **European Sleeper** night trains.

Timetable: **1 Jun 2026 – 8 Nov 2026** · 6 routes · 23 trip variants · 36 stops · 657 service dates

## Download

[gtfs-european-sleeper.zip](./gtfs-european-sleeper.zip)

## Trains

| Train | Route | Notes |
|-------|-------|-------|
| ES 452 | Prague → Brussels | 4 variants (Amsterdam rerouting mid-Jun, Berlin Gesundbrunnen from 14 Jun, Arnhem diversion Jun-Sep) |
| ES 453 | Brussels → Prague | 4 variants (mirrors ES 452) |
| ES 474 | Berlin → Paris | 7 variants (Berlin-Spandau diversion mid-Jun, Hamburg added 13 Jul) |
| ES 475 | Paris → Berlin | 6 variants (Berlin Gesundbrunnen Jun, Hamburg added 12 Jul) |
| ES 400 | Milan → Brussels | 1 variant, starts 9 Sep 2026 |
| ES 401 | Brussels → Milan | 1 variant, starts 9 Sep 2026 |

Uses `calendar_dates.txt` (explicit per-date entries) to capture all timetable variations correctly.

## Generating

Requires Python 3.10+.

```bash
pip install requests
python3 generate.py
```

Fetches live timetable data from the European Sleeper website (~1200 requests, ~2 min).
The feed is also regenerated automatically every Monday via GitHub Actions.

## Sources

- [European Sleeper timetable](https://www.europeansleeper.eu/en/timetable) (live data via `/timetable/run` API)
- [OpenStreetMap](https://www.openstreetmap.org) (station coordinates, ODbL)

## License

Feed: [CC0](https://creativecommons.org/publicdomain/zero/1.0/) · Coordinates: © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/)
