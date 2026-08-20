# gtfs-european-sleeper

A [GTFS](https://gtfs.org) feed for European Sleeper night trains, built from data European Sleeper doesn't publish as GTFS themselves.

## Why this exists

European Sleeper only publishes a timetable page meant for humans. There's no official GTFS feed, so this scrapes and reassembles one: which trains run on which days, what stops they make, and where those stops actually are.

This feed also feeds into [Panto](https://getpanto.app), a real-time train tracking app currently in beta.

## Download

[gtfs-european-sleeper.zip](./gtfs-european-sleeper.zip)

## Trains

European Sleeper's night trains connect stations like Amsterdam Centraal, Brussels Midi, Berlin Hauptbahnhof, Prague hl.n., Paris Nord and Milano Garibaldi. The exact lineup of routes shifts as they add or drop connections, so this doesn't hardcode a list of them anywhere.

Each train can also run several different stop patterns across a season: reroutes, added or dropped stops, seasonal detours. The feed captures all of them as separate trip variants tied to the specific dates they ran.

## Where the data comes from

| Source | What it provides |
|--------|-------------------|
| `europeansleeper.eu/timetable/run` | The endpoint behind their timetable page. Not documented anywhere, found by watching the network tab while using the site. Returns per-day HTML for every route in one call, which this scrapes for stop names and times. |
| `europeansleeperprod-api.azurewebsites.net/api/constants` | The booking backend's config endpoint. Gives the UIC code for every station ES sells tickets to and from, straight from the operator instead of guessed from a third party. |
| [Wikidata](https://www.wikidata.org) | Coordinates and timezone for every stop, plus UIC codes for detour/seasonal stations that don't show up in the booking API. |

Cross-checking the booking API against Wikidata turned up a couple of wrong UIC codes on Wikidata itself (Berlin Hbf, Paris Nord), worth knowing if you're pulling station data from there for anything else in this corridor.

## Generating

Requires Python 3.10+.

```bash
pip install requests
python3 generate.py
```

Scans the current season day by day, then resolves every station name it found. Takes a few minutes. Runs automatically every Monday via GitHub Actions.

## Limitations

- No fares: European Sleeper's pricing is dynamic and this feed is only rebuilt weekly, so baking in prices would just mean shipping stale ones.
- No shapes: there's no route geometry source that doesn't involve map-matching against OSM, which felt out of scope for what's otherwise a schedule feed.
- Station names come from whatever European Sleeper's site returns that day. Ambiguous matches against Wikidata get logged rather than silently guessed.

## License

Feed: [CC0](https://creativecommons.org/publicdomain/zero/1.0/). Station data: © Wikidata contributors, CC0.

Not affiliated with European Sleeper Exploitatie B.V. or the European Sleeper Coöperatie U.A.
