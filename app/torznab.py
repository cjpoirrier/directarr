import re

from fastapi.responses import Response
from xml.etree.ElementTree import Element, SubElement, tostring

from .db import connect

NS = "http://torznab.com/schemas/2015/feed"


def _normalize(text: str) -> str:
    """Lowercase and strip punctuation so 'rezero' matches 'Re:ZERO'."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def caps():
    root = Element("caps")
    searching = SubElement(root, "searching")
    SubElement(searching, "search", available="yes")
    SubElement(searching, "tv-search", available="yes")
    SubElement(searching, "movie-search", available="no")
    cats = SubElement(root, "categories")
    category = SubElement(cats, "category", id="5000", name="TV")
    SubElement(category, "category", id="5070", name="Anime")
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/xml",
    )


def results(query, season=None, episode=None):
    root = Element("rss", version="2.0")
    root.set("xmlns:torznab", NS)
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = "Directarr"
    SubElement(channel, "description").text = "Direct-download search results"

    sql = "SELECT * FROM releases WHERE 1=1"
    args: list = []
    if season is not None:
        sql += " AND season=?"
        args.append(season)
    if episode is not None:
        sql += " AND episode=?"
        args.append(episode)
    sql += " ORDER BY created_at DESC LIMIT 500"

    needle = _normalize(query)
    with connect() as db:
        rows = db.execute(sql, args).fetchall()

    matched = []
    for row in rows:
        if needle and needle not in _normalize(row["title"]):
            continue
        matched.append(row)
        if len(matched) >= 100:
            break

    for row in matched:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = row["title"]
        SubElement(item, "guid", isPermaLink="false").text = row["id"]
        SubElement(item, "link").text = row["url"]
        SubElement(item, "category").text = "Anime"
        SubElement(item, "description").text = "Source: " + row["source"]
        attributes = [("category", "5070")]
        for key in ("season", "episode", "resolution", "codec", "size"):
            if row[key] is not None:
                attributes.append((key, str(row[key])))
        for name, value in attributes:
            SubElement(item, f"{{{NS}}}attr", name=name, value=value)
    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/rss+xml",
    )
