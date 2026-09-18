"""Pull the history the replay harness needs straight from Home Assistant.

Clicking twenty exports through the History panel is the slow way (Anze,
2026-09-18). The REST API returns the same states, so this walks a day at a
time over each group of entities and writes one CSV per day per group, in
exactly the shape the panel downloads - entity_id, state, last_changed - so
replay.py reads either without knowing the difference.

    python3 tests/fetch_history.py --days 10
    python3 tests/fetch_history.py --days 1 --ending 2026-09-14 --chunk-hours 6

It needs a long-lived access token, which it reads from a FILE and never
takes on the command line, where it would land in a shell history:

    Home Assistant, your profile, Security, Long-lived access tokens.
    Save it as data/<hostname> - so data/ha.alpacasbarn.com holds the token
    that instance issued. The whole data/ folder is gitignored.

A token belongs to ONE instance, so each site needs its own, and naming the
file after the host is what keeps them straight (Anze, 2026-09-18).

Days already downloaded are skipped, so an interrupted run resumes.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_urls import SITES  # noqa: E402  - one place for the entity lists

ROOT = Path(__file__).resolve().parents[1]


def read_token(path: Path) -> str:
    if not path.exists():
        raise SystemExit(
            f"no token at {path}\n"
            "  Home Assistant -> your profile -> Security -> Long-lived access tokens,\n"
            "  then save it at that path - the file is named after the host it\n"
            "  came from. One per instance; data/ is gitignored."
        )
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"{path} is empty")
    return token


def fetch(base: str, token: str, entities, start: datetime, end: datetime, timeout: int):
    """The API's history for one window, as (entity_id, state, last_changed)."""
    query = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entities),
        "end_time": end.astimezone(timezone.utc).isoformat(),
        "minimal_response": "",
        "no_attributes": "",
    })
    stamp = start.astimezone(timezone.utc).isoformat()
    url = f"{base}/api/history/period/{urllib.parse.quote(stamp)}?{query}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    rows = []
    for series in payload:
        # with minimal_response only the first entry carries the entity id
        entity_id = series[0].get("entity_id") if series else None
        for entry in series:
            entity_id = entry.get("entity_id") or entity_id
            when = entry.get("last_changed") or entry.get("last_updated")
            if entity_id and when is not None:
                rows.append((entity_id, entry.get("state"), when))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="home", choices=sorted(SITES))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--ending", help="last local day, YYYY-MM-DD; default yesterday")
    parser.add_argument("--token", help="default data/<hostname of the site>")
    parser.add_argument("--out", help="default data/history/<site>")
    parser.add_argument("--chunk-hours", type=int, default=24,
                        help="split each day, for entities that report every second")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="say what it would fetch")
    args = parser.parse_args()

    site = SITES[args.site]
    tz = site["tz"]
    out = Path(args.out) if args.out else ROOT / "data" / "history" / args.site
    out.mkdir(parents=True, exist_ok=True)
    given = Path(args.token) if args.token else None
    if given is not None:
        token_path = given if given.is_absolute() else ROOT / given
    else:
        host = urllib.parse.urlsplit(site["base"]).hostname or args.site
        candidates = [ROOT / "data" / host, ROOT / "data" / f"ha_token_{args.site}",
                      ROOT / "data" / "ha_token"]
        token_path = next((p for p in candidates if p.exists()), candidates[0])
    token = "" if args.dry_run else read_token(token_path)

    last = (datetime.strptime(args.ending, "%Y-%m-%d").replace(tzinfo=tz) if args.ending
            else datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    days = [last - timedelta(days=n) for n in range(args.days - 1, -1, -1)]

    total = 0
    for day in days:
        for group, entities in site["groups"].items():
            target = out / f"{day:%Y-%m-%d}_{group}.csv"
            if target.exists():
                print(f"  {target.name}: already here")
                continue
            rows = []
            edge = day
            while edge < day + timedelta(days=1):
                stop = min(edge + timedelta(hours=args.chunk_hours), day + timedelta(days=1))
                if args.dry_run:
                    print(f"  would fetch {len(entities)} entities "
                          f"{edge:%Y-%m-%d %H:%M} -> {stop:%H:%M}")
                else:
                    try:
                        rows += fetch(site["base"], token, entities, edge, stop, args.timeout)
                    except urllib.error.HTTPError as exc:
                        print(f"  {target.name}: HTTP {exc.code} {exc.reason}")
                        return 1
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {target.name}: {type(exc).__name__}: {exc}")
                        return 1
                edge = stop
            if args.dry_run:
                continue
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["entity_id", "state", "last_changed"])
                writer.writerows(rows)
            total += len(rows)
            print(f"  {target.name}: {len(rows)} rows")
    if not args.dry_run:
        print(f"{total} rows into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
