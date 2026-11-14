import re
from datetime import datetime, timezone
from email.utils import format_datetime

from fastapi.responses import Response
from xml.etree.ElementTree import Element, SubElement, tostring

from .db import connect
from .parser import episode_matches_row, title_matches_query

NS = "http://torznab.com/schemas/2015/feed"


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation so 'rezero' matches 'Re:ZERO'."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def caps():
    root = Element("caps")
    searching = SubElement(root, "searching")
    SubElement(
        searching,
        "search",
        available="yes",
        supportedParams="q",
    )
    SubElement(
        searching,
        "tv-search",
        available="yes",
        supportedParams="q,season,ep",
    )
    SubElement(searching, "movie-search", available="no")
    cats = SubElement(root, "categories")
    category = SubElement(cats, "category", id="5000", name="TV")
    SubElement(category, "category", id="5070", name="Anime")
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


def results(query, season=None, episode=None, base_url: str = ""):
    """Build Torznab RSS for Sonarr.

    Matching rules (tvsearch):
    - Title must loosely match the series query
    - Episode must match S/E, or be a season pack for that season
    - Season packs are tagged with the requested S/E so Sonarr accepts them
    """
    root = Element("rss", version="2.0")
    root.set("xmlns:torznab", NS)
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = "Directarr"
    SubElement(channel, "description").text = "Direct-download search results"

    sql = "SELECT * FROM releases ORDER BY created_at DESC LIMIT 800"
    with connect() as db:
        rows = db.execute(sql).fetchall()

    matched = []
    for row in rows:
        title = row["title"] or ""
        if not title_matches_query(title, query):
            continue
        row_season = row["season"]
        row_episode = row["episode"]
        if not episode_matches_row(
            row_season=row_season,
            row_episode=row_episode,
            title=title,
            want_season=season,
            want_episode=episode,
            allow_season_pack=True,
        ):
            continue
        matched.append(row)
        if len(matched) >= 100:
            break

    now = format_datetime(datetime.now(timezone.utc))

    for row in matched:
        item = SubElement(channel, "item")
        title = row["title"] or ""
        rid = row["id"]
        SubElement(item, "title").text = title
        SubElement(item, "guid", isPermaLink="false").text = rid
        # Grab endpoint so Sonarr's download request queues a Directarr job
        grab_url = f"{base_url.rstrip('/')}/api/v1/grab/{rid}" if base_url else f"/api/v1/grab/{rid}"
        SubElement(item, "link").text = grab_url
        SubElement(item, "category").text = "Anime"
        SubElement(item, "description").text = "Source: " + (row["source"] or "")
        SubElement(item, "pubDate").text = now

        size = int(row["size"] or 0)
        enc = SubElement(item, "enclosure")
        enc.set("url", grab_url)
        enc.set("length", str(size if size > 0 else 1))
        enc.set("type", "application/x-bittorrent")

        # Sonarr uses these attrs heavily. For season packs, advertise the
        # requested episode so the result is eligible for that search.
        out_season = row["season"]
        out_episode = row["episode"]
        if season is not None and episode is not None:
            if out_episode is None and out_season == season:
                out_season = season
                out_episode = episode
            elif out_season is None and out_episode is None:
                out_season = season
                out_episode = episode
        elif season is not None and out_season is None:
            out_season = season

        attributes = [("category", "5070"), ("seeders", "1"), ("peers", "1")]
        seen_attr = set(["category", "seeders", "peers"])
        if out_season is not None:
            attributes.append(("season", str(int(out_season))))
            seen_attr.add("season")
        if out_episode is not None:
            attributes.append(("episode", str(int(out_episode))))
            seen_attr.add("episode")
        for key in ("resolution", "codec", "size"):
            if row[key] is not None and key not in seen_attr:
                attributes.append((key, str(row[key])))
                seen_attr.add(key)
        for name, value in attributes:
            SubElement(item, f"{{{NS}}}attr", name=name, value=value)
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/rss+xml",
    )