import re
import uuid

from .db import connect

_DUAL_AUDIO_RE = re.compile(
    r"(?i)(dual[\s._-]*audio|multi[\s._-]*audio|dual[\s._-]*lang|multi[\s._-]*lang|"
    r"\bDUAL\b|\bMULTI\b|eng(?:lish)?[\s._+-]*jap(?:anese)?|jap(?:anese)?[\s._+-]*eng(?:lish)?)"
)

# S01E02, S1E2, S01.E02, S01_E02
_SE_RE = re.compile(
    r"(?i)\bS(\d{1,2})\s*[.\-_\s]*E(\d{1,3})\b"
)
# 1x02
_X_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")
# Episode 02 / Ep02 / EP.02
_EP_RE = re.compile(r"(?i)\b(?:Episode|Episodes|Ep|E)[.\s_-]*(\d{1,3})\b")
# Standalone padded episode near separators: - 01 -, [01]
_SOLO_EP_RE = re.compile(
    r"(?i)(?:^|[\s\[\(\-_.])0*(\d{1,3})(?:[\s\]\)\-_.]|$)"
)
# Season pack markers
_SEASON_PACK_RE = re.compile(
    r"(?i)\b(?:season|seasons)\s*(\d{1,2})\b|\bS(\d{1,2})\b(?!\s*[.\-_]?\s*E\d)"
)
_PACK_HINT_RE = re.compile(
    r"(?i)\b(complete|batch|pack|season\s*pack|full\s*season|whole\s*season)\b"
)


def enrich(title: str):
    """Extract season, episode, resolution, codec, dual_audio, is_season_pack."""
    season = None
    episode = None
    is_pack = bool(_PACK_HINT_RE.search(title or ""))

    match = _SE_RE.search(title or "")
    if not match:
        match = _X_RE.search(title or "")
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
        ep_m = _EP_RE.search(title or "")
        if ep_m:
            episode = int(ep_m.group(1))
        pack_m = _SEASON_PACK_RE.search(title or "")
        if pack_m:
            season = int(pack_m.group(1) or pack_m.group(2))
            is_pack = True

    # Heuristic: "Show - 01" style only if we still lack episode and not clearly a pack
    if episode is None and not is_pack:
        for m in _SOLO_EP_RE.finditer(title or ""):
            n = int(m.group(1))
            if 1 <= n <= 64:  # sane episode range; skip years
                episode = n
                break

    low = (title or "").lower()
    resolution = next((v for v in ("2160p", "1080p", "720p", "480p") if v in low), None)
    codec = next(
        (v for v in ("AV1", "HEVC", "H.265", "x265", "H.264", "x264") if v.lower() in low),
        None,
    )
    dual_audio = bool(_DUAL_AUDIO_RE.search(title or ""))
    return season, episode, resolution, codec, dual_audio, is_pack


def format_episode_tag(season: int | None, episode: int | None) -> str:
    if season is not None and episode is not None:
        return f"S{int(season):02d}E{int(episode):02d}"
    if episode is not None:
        return f"E{int(episode):02d}"
    if season is not None:
        return f"S{int(season):02d}"
    return ""


def build_search_queries(
    q: str,
    *,
    season: int | None = None,
    episode: int | None = None,
) -> list[str]:
    """Build ordered unique queries for source adapters (Sonarr-friendly)."""
    base = (q or "").strip()
    out: list[str] = []

    def add(s: str) -> None:
        s = " ".join((s or "").split())
        if s and s.lower() not in {x.lower() for x in out}:
            out.append(s)

    add(base)
    if not base:
        return out

    tag = format_episode_tag(season, episode)
    if tag:
        add(f"{base} {tag}")
        if season is not None and episode is not None:
            add(f"{base} S{int(season)}E{int(episode)}")
            add(f"{base} {int(season)}x{int(episode):02d}")
            add(f"{base} Episode {int(episode)}")
            add(f"{base} Season {int(season)}")
        elif season is not None:
            add(f"{base} Season {int(season)}")
            add(f"{base} S{int(season):02d}")
        elif episode is not None:
            add(f"{base} Episode {int(episode)}")
            add(f"{base} {int(episode):02d}")

    return out


def title_matches_query(title: str, query: str) -> bool:
    """Loose title match: normalized substring OR all query tokens present."""
    if not query or not query.strip():
        return True
    nt = re.sub(r"[^a-z0-9]+", "", (title or "").lower())
    nq = re.sub(r"[^a-z0-9]+", "", (query or "").lower())
    if nq and nq in nt:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(t) >= 2]
    if not tokens:
        return True
    title_l = (title or "").lower()
    title_norm = re.sub(r"[^a-z0-9]+", " ", title_l)
    hits = sum(1 for t in tokens if t in title_norm or t in nt)
    need = max(1, len(tokens) - (1 if len(tokens) >= 4 else 0))
    return hits >= need


def episode_matches_row(
    *,
    row_season: int | None,
    row_episode: int | None,
    title: str,
    want_season: int | None,
    want_episode: int | None,
    allow_season_pack: bool = True,
) -> bool:
    """Whether a release satisfies a Sonarr tvsearch season/ep request."""
    if want_season is None and want_episode is None:
        return True

    ts, te, _, _, _, title_pack = enrich(title)
    s = row_season if row_season is not None else ts
    e = row_episode if row_episode is not None else te
    is_pack = title_pack or (
        s is not None and e is None and bool(_PACK_HINT_RE.search(title or ""))
    )

    if want_season is not None and want_episode is not None:
        if s == want_season and e == want_episode:
            return True
        tag = format_episode_tag(want_season, want_episode)
        compact = re.sub(r"[\s._\-]", "", (title or "").lower())
        if tag and tag.lower() in compact:
            return True
        # Alternate S1E1 without zero padding
        alt = f"s{int(want_season)}e{int(want_episode)}"
        if alt in compact:
            return True
        if allow_season_pack and is_pack and s == want_season and e is None:
            return True
        if allow_season_pack and s == want_season and e is None and _PACK_HINT_RE.search(
            title or ""
        ):
            return True
        return False

    if want_season is not None:
        return s == want_season

    if want_episode is not None:
        return e == want_episode

    return True


def save_release(release):
    season, episode, resolution, codec, _dual, _pack = enrich(release.title)
    if getattr(release, "season", None) is not None:
        season = release.season
    if getattr(release, "episode", None) is not None:
        episode = release.episode
    if getattr(release, "resolution", None):
        resolution = release.resolution or resolution
    if getattr(release, "codec", None):
        codec = release.codec or codec

    rid = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            release.source + "|" + release.title + "|" + release.url,
        )
    )
    with connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO releases "
            "(id,title,url,source,size,publish_date,season,episode,resolution,codec) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                release.title,
                release.url,
                release.source,
                release.size,
                release.publish_date,
                season,
                episode,
                resolution,
                codec,
            ),
        )
    return rid
