import re
from datetime import datetime, timezone
from email.utils import format_datetime

from fastapi.responses import Response
from xml.etree.ElementTree import Element, SubElement, tostring

from .db import connect
from .parser import enrich, episode_matches_row, title_matches_query

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


def _file_matches(name: str, want_season, want_episode) -> bool:
    """True if a resolved Drive filename matches the requested S/E."""
    if want_season is None and want_episode is None:
        return True
    s, e, _, _, _, _ = enrich(name or "")
    if want_season is not None and want_episode is not None:
        if s == want_season and e == want_episode:
            return True
        # Common patterns: S01E01, 1x01, E01, - 01., [01]
        compact = re.sub(r"[\s._\-\[\]()]", "", (name or "").lower())
        tag = f"s{int(want_season):02d}e{int(want_episode):02d}"
        tag2 = f"s{int(want_season)}e{int(want_episode)}"
        x_tag = f"{int(want_season)}x{int(want_episode):02d}"
        if tag in compact or tag2 in compact or x_tag in compact:
            return True
        # Bare episode number with word boundaries when season is 1 and title has S01 or Season 1 context is weak;
        # only accept if the name clearly contains the episode token and not a pack marker.
        if re.search(rf"(?i)(?:^|[\s._\-])0*{int(want_episode)}(?:[\s._\-]|$)|\be{int(want_episode):02d}\b", name or ""):
            if not re.search(r"(?i)season\s*\d+\s*-\s*\d+|complete|batch|pack|ovas", name or ""):
                return True
        return False
    if want_season is not None:
        return s == want_season
    if want_episode is not None:
        return e == want_episode
    return True


def _try_resolve_files(url: str):
    """Return list of {name, id, size, url} from a Drive share, or empty list.

    Handles both DriveItem objects and plain dicts from resolve_url.
    """
    try:
        from . import gdrive
        if not gdrive.is_drive_url(url):
            return []
        status = gdrive.credentials_status()
        if not status.get("authorized"):
            return []
        resolved = gdrive.resolve_url(url, recursive=True)
        files = resolved.get("files") or []
        out = []
        for f in files:
            if isinstance(f, dict):
                if f.get("is_folder"):
                    continue
                name = f.get("name") or ""
                fid = f.get("id") or ""
                size = f.get("size") or 0
            else:
                if getattr(f, "is_folder", False):
                    continue
                name = getattr(f, "name", "") or ""
                fid = getattr(f, "id", "") or ""
                size = getattr(f, "size", None) or 0
            if name and fid:
                out.append({
                    "name": name,
                    "id": fid,
                    "size": int(size) if size else 0,
                    "url": url,
                })
        return out
    except Exception:
        return []


def results(query, season=None, episode=None, base_url: str = ""):
    """Build Torznab RSS for Sonarr.

    Episode-specific searches resolve Drive folders and emit only the matching
    individual files (real sizes). Packs are returned only when no specific
    episode was requested, or as a fallback if resolution finds nothing.
    """
    root = Element("rss", version="2.0")
    root.set("xmlns:torznab", NS)
    channel = SubElement(root, "channel")
    SubElement(channel, "title").text = "Directarr"
    SubElement(channel, "description").text = "Direct-download search results"

    sql = "SELECT * FROM releases ORDER BY created_at DESC LIMIT 800"
    with connect() as db:
        rows = db.execute(sql).fetchall()

    episode_search = season is not None and episode is not None
    matched = []
    for row in rows:
        title = row["title"] or ""
        if not title_matches_query(title, query):
            continue
        # For episode searches allow packs as candidates so we can resolve them;
        # for other searches use normal matching.
        if not episode_matches_row(
            row_season=row["season"],
            row_episode=row["episode"],
            title=title,
            want_season=season,
            want_episode=episode,
            allow_season_pack=True,
        ):
            continue
        matched.append(row)
        if len(matched) >= 40:  # keep resolve budget reasonable
            break

    now = format_datetime(datetime.now(timezone.utc))
    items_emitted = 0
    seen_file_ids = set()

    for row in matched:
        title = row["title"] or ""
        rid = row["id"]
        url = row["url"] or ""
        source = row["source"] or ""

        # Episode search: prefer resolved individual files
        resolved_files = []
        if episode_search:
            resolved_files = [
                f for f in _try_resolve_files(url)
                if _file_matches(f["name"], season, episode)
            ]

        if resolved_files:
            for f in resolved_files:
                if f["id"] in seen_file_ids:
                    continue
                seen_file_ids.add(f["id"])
                grab_url = (
                    f"{base_url.rstrip('/')}/api/v1/grab/{rid}"
                    f"?drive_file_id={f['id']}&title={f['name']}"
                    if base_url
                    else f"/api/v1/grab/{rid}?drive_file_id={f['id']}&title={f['name']}"
                )
                item = SubElement(channel, "item")
                SubElement(item, "title").text = f["name"]
                SubElement(item, "guid", isPermaLink="false").text = f"{rid}:{f['id']}"
                SubElement(item, "link").text = grab_url
                SubElement(item, "category").text = "Anime"
                SubElement(item, "description").text = f"Source: {source} (resolved file)"
                SubElement(item, "pubDate").text = now
                enc = SubElement(item, "enclosure")
                enc.set("url", grab_url)
                enc.set("length", str(f["size"] if f["size"] > 0 else 1))
                enc.set("type", "application/x-bittorrent")
                attrs = [
                    ("category", "5070"),
                    ("seeders", "1"),
                    ("peers", "1"),
                    ("season", str(int(season))),
                    ("episode", str(int(episode))),
                ]
                if f["size"] > 0:
                    attrs.append(("size", str(f["size"])))
                for name, value in attrs:
                    SubElement(item, f"{{{NS}}}attr", name=name, value=value)
                items_emitted += 1
                if items_emitted >= 100:
                    break
            if items_emitted >= 100:
                break
            continue  # do not also emit the pack when we have individual files

        # No resolved files (or not an episode search): emit the release itself
        # Only skip pure packs on episode searches when we already emitted files
        # for other candidates; still emit if nothing else matched.
        if episode_search and items_emitted > 0:
            # We already have concrete episode files; skip remaining packs
            continue

        grab_url = f"{base_url.rstrip('/')}/api/v1/grab/{rid}" if base_url else f"/api/v1/grab/{rid}"
        item = SubElement(channel, "item")
        SubElement(item, "title").text = title
        SubElement(item, "guid", isPermaLink="false").text = rid
        SubElement(item, "link").text = grab_url
        SubElement(item, "category").text = "Anime"
        SubElement(item, "description").text = "Source: " + source
        SubElement(item, "pubDate").text = now
        size = int(row["size"] or 0)
        enc = SubElement(item, "enclosure")
        enc.set("url", grab_url)
        enc.set("length", str(size if size > 0 else 1))
        enc.set("type", "application/x-bittorrent")

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
        seen_attr = {"category", "seeders", "peers"}
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
        items_emitted += 1
        if items_emitted >= 100:
            break

    return Response(
        tostring(root, encoding="utf-8", xml_declaration=True),
        media_type="application/rss+xml",
    )
