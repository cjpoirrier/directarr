import re, uuid
from .db import connect

def enrich(title: str):
    # Common anime/TV patterns: S01E02, S1E2, 1x02, Episode 02, Ep02
    match = re.search(r"(?i)S(\d{1,2})\s*E(\d{1,3})", title)
    if not match:
        match = re.search(r"(?i)(\d{1,2})x(\d{1,3})", title)
    if not match:
        match = re.search(r"(?i)(?:Episode|Ep)[.\s_-]*(\d{1,3})", title)
        season = None
        episode = int(match.group(1)) if match else None
    else:
        season = int(match.group(1))
        episode = int(match.group(2))
    resolution = next((v for v in ("2160p", "1080p", "720p", "480p") if v.lower() in title.lower()), None)
    codec = next((v for v in ("AV1", "HEVC", "H.265", "x265", "H.264", "x264") if v.lower() in title.lower()), None)
    return season, episode, resolution, codec

def save_release(release):
    season, episode, resolution, codec = enrich(release.title)
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, release.source + "|" + release.title + "|" + release.url))
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO releases (id,title,url,source,size,publish_date,season,episode,resolution,codec) VALUES (?,?,?,?,?,?,?,?,?,?)",
          (rid, release.title, release.url, release.source, release.size, release.publish_date, season, episode, resolution, codec))
    return rid
