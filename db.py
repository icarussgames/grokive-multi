"""SQLite read-model for the gallery.

This is a *derived* index built from ``metadata.json`` (the download ledger) plus
what's on disk (thumbnail dimensions, subtitle sidecars). It is safe to delete and
rebuild at any time; the downloader never writes here. The web API queries this DB
instead of shipping the whole library to the browser, which gives fast paginated
results and real full-text prompt search (FTS5).

Favorites / archive / playlists still live in their JSON files for now and are
applied as filters by the API layer; a later phase can fold them into tables here.

Tags come from two sources, told apart by ``media_tags.source``: ``auto`` tags are
lifted from prompts at index time (mediautil.tags_for_groups), ``user`` tags are the
ones the user assigns by hand. User tags are DURABLE state in ``tags.json`` (owned by
the server); the index only mirrors them so search / filter / facets can use them.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from mediautil import TAG_WORDS_FILE, group_key_and_label, load_tag_words, media_rel_path, media_shard, tags_for_groups

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow always present in practice
    Image = None  # type: ignore


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
  id                TEXT PRIMARY KEY,
  media_type        TEXT,
  prompt            TEXT,
  normalized_prompt TEXT,
  model             TEXT,
  created_at        TEXT,
  parent_id         TEXT,
  source_url        TEXT,
  local_path        TEXT,
  href              TEXT,
  thumb             TEXT,
  subtitles         TEXT,
  canvas_id         TEXT,
  canvas_name       TEXT,
  thumb_w           INTEGER,
  thumb_h           INTEGER,
  media_w           INTEGER,
  media_h           INTEGER,
  has_subtitles     INTEGER DEFAULT 0,
  size_bytes        INTEGER,
  api_generated     INTEGER DEFAULT 0,
  preset            TEXT,
  is_root_celebrity INTEGER,
  r_rated           INTEGER,
  moderated         INTEGER,
  is_ext            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);
CREATE INDEX IF NOT EXISTS idx_media_type    ON media(media_type);
CREATE INDEX IF NOT EXISTS idx_media_canvas  ON media(canvas_id);
CREATE INDEX IF NOT EXISTS idx_media_model   ON media(model);

CREATE TABLE IF NOT EXISTS media_tags (
  media_id TEXT,
  tag      TEXT,
  source   TEXT NOT NULL DEFAULT 'auto',
  PRIMARY KEY (media_id, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON media_tags(tag);

CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
  id UNINDEXED, prompt, tags, model, filename, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Grok's per-asset flags (gdownloader.AUX_FLAG_KEYS). Stored nullable: SQLite has no bool,
# and NULL is load-bearing here -- it means "never captured", which covers the whole library
# archived before these existed. Only 1/0 are Grok's own answer.
AUX_FLAG_COLUMNS = ("is_root_celebrity", "r_rated", "moderated", "is_ext")

MEDIA_COLUMNS = [
    "id", "media_type", "prompt", "normalized_prompt", "model", "created_at",
    "parent_id", "source_url", "local_path", "href", "thumb", "subtitles",
    "canvas_id", "canvas_name", "thumb_w", "thumb_h", "media_w", "media_h",
    "has_subtitles", "size_bytes", "api_generated", "preset",
    *AUX_FLAG_COLUMNS,
]


# Durable user-tag state (see the module docstring), a sibling of metadata.json.
USER_TAGS_FILE = "tags.json"
TAG_SOURCES = ("auto", "user")


def load_user_tag_items(path: str | Path) -> dict[str, list[str]]:
    """``{media_id: [tag, ...]}`` from a ``tags.json`` file. Tolerant: a missing or
    unreadable file reads as no user tags (the server owns validation + strict writes)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    out: dict[str, list[str]] = {}
    for mid, tags in items.items():
        if isinstance(tags, list):
            clean = [t for t in (str(x).strip() for x in tags) if t]
            if clean:
                out[str(mid)] = list(dict.fromkeys(clean))
    return out


def _ensure_tag_schema(conn: sqlite3.Connection) -> None:
    """Migrate a pre-``source`` media_tags table. The table is purely derived (every
    build refills it), so the old one is simply dropped and recreated."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_tags)")}
    if cols and "source" not in cols:
        conn.execute("DROP TABLE media_tags")
        conn.execute(
            "CREATE TABLE media_tags (media_id TEXT, tag TEXT, source TEXT NOT NULL DEFAULT 'auto', "
            "PRIMARY KEY (media_id, tag, source))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag ON media_tags(tag)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_source_tag ON media_tags(source, tag)")


def _tag_sources(tag_source: str) -> tuple[str, ...]:
    """Which ``media_tags.source`` values a tag filter matches: 'user', 'auto' or both."""
    return (tag_source,) if tag_source in TAG_SOURCES else TAG_SOURCES


def _tag_filter_clause(tags: Iterable[str], tag_source: str = "all", tag_mode: str = "any",
                       alias: str = "mt") -> tuple[str | None, list[Any]]:
    """SQL (and params) restricting ``m`` to media carrying the tags. ``any`` (default) is
    match-ANY — one EXISTS over the whole list; ``all`` requires every tag (one EXISTS each)."""
    tl = [t for t in tags if t]
    if not tl:
        return None, []
    sources = _tag_sources(tag_source)
    src_sql = ",".join("?" for _ in sources)
    if tag_mode == "all":
        parts = [f"EXISTS (SELECT 1 FROM media_tags {alias} WHERE {alias}.media_id = m.id "
                 f"AND {alias}.tag = ? AND {alias}.source IN ({src_sql}))" for _ in tl]
        params: list[Any] = []
        for t in tl:
            params.extend([t, *sources])
        return "(" + " AND ".join(parts) + ")", params
    ph = ",".join("?" for _ in tl)
    return (f"EXISTS (SELECT 1 FROM media_tags {alias} WHERE {alias}.media_id = m.id "
            f"AND {alias}.tag IN ({ph}) AND {alias}.source IN ({src_sql}))"), [*tl, *sources]


def _attach_tags(conn: sqlite3.Connection, d: dict[str, Any]) -> dict[str, Any]:
    """Prompt-derived ``tags`` and hand-assigned ``user_tags``, kept apart."""
    d["tags"], d["user_tags"] = [], []
    for tag, source in conn.execute(
        "SELECT tag, source FROM media_tags WHERE media_id = ? ORDER BY tag COLLATE NOCASE", (d["id"],)
    ):
        (d["user_tags"] if source == "user" else d["tags"]).append(tag)
    return d


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Writers can briefly collide (a background rebuild vs. delete_media on a
    # request thread); wait out the other writer instead of failing with
    # "database is locked". build_index keeps its write transaction short so
    # this ceiling is never approached.
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _thumb_dims(path: Path) -> tuple[int | None, int | None]:
    if Image is None or not path.exists():
        return None, None
    try:
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _video_dims(path: Path) -> tuple[int | None, int | None]:
    """Source video pixel dimensions via ffprobe (None,None if unavailable)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path.exists():
        return None, None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        w, _, h = out.partition("x")
        return int(w), int(h)
    except Exception:
        return None, None


def _media_dims(media_type: str | None, path: Path) -> tuple[int | None, int | None]:
    """Real source dimensions of a media file: ffprobe for video, Pillow for images."""
    if media_type == "video":
        return _video_dims(path)
    return _thumb_dims(path)  # any image format Pillow can open


def _rel_href(local_path: str, gallery_dir: Path) -> str:
    """Browser-relative path to a media file, matching how the gallery serves it
    (``/media/...`` and ``/thumbnails/...`` under the gallery root)."""
    return local_path.replace("\\", "/")


def build_index(
    db_path: str | Path,
    metadata_path: str | Path,
    gallery_dir: str | Path,
    thumbnails_dir: str | Path | None = None,
    user_tags_path: str | Path | None = None,
) -> int:
    """(Re)build the index from metadata.json. Returns the number of rows written.

    User tags are read from ``user_tags_path`` (default: ``tags.json`` next to
    metadata.json) INSIDE the table-swap write transaction, so a tag edit that lands
    while a rebuild is scanning is never overwritten by a stale snapshot."""
    gallery_dir = Path(gallery_dir)
    thumbnails_dir = Path(thumbnails_dir) if thumbnails_dir else gallery_dir / "thumbnails"
    metadata_path = Path(metadata_path)
    items: list[dict[str, Any]] = []
    if metadata_path.exists():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                items = [it for it in loaded if isinstance(it, dict) and it.get("id")]
        except Exception:
            items = []

    # Group by normalized prompt and compute group-level tags, exactly like the
    # static gallery, then assign each group's tags to its member media.
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        key, label = group_key_and_label(item)
        if key not in buckets:
            buckets[key] = {"prompt": label, "ids": []}
            order.append(key)
        buckets[key]["ids"].append(item["id"])
    groups = [{"prompt": buckets[k]["prompt"]} for k in order]
    # The data dir's optional tag_words.json extends the tagger's neutral built-in word lists
    # (library-specific vocabulary is kept out of the repo and backed up with the data).
    tags_for_groups(groups, load_tag_words(metadata_path.parent / TAG_WORDS_FILE))
    tags_by_id: dict[str, list[str]] = {}
    for key, group in zip(order, groups):
        for mid in buckets[key]["ids"]:
            tags_by_id[mid] = group.get("tags", [])

    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_tag_schema(conn)
        # Migrate pre-existing DBs: CREATE TABLE IF NOT EXISTS won't add new columns.
        for col in ("media_w", "media_h", "size_bytes"):
            try:
                conn.execute(f"ALTER TABLE media ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # already present
        # Provenance flag for API-generated media (Grok Imagine). DEFAULT 0 so existing
        # rows read as "not generated".
        try:
            conn.execute("ALTER TABLE media ADD COLUMN api_generated INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # already present
        # Beat-montage style (preset id, e.g. "musicvideo") — provenance surfaced in the
        # lightbox info panel. NULL for everything that isn't a montage.
        try:
            conn.execute("ALTER TABLE media ADD COLUMN preset TEXT")
        except sqlite3.OperationalError:
            pass  # already present
        # Grok's per-asset content/provenance flags. Deliberately NO DEFAULT: these are
        # only captured off the conversations route and only since they were added, so
        # NULL has to keep meaning "unknown" for the whole existing library. Defaulting
        # to 0 would assert Grok rated 25k historical items, which it never did.
        for col in AUX_FLAG_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE media ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass  # already present
        # Carry probed dimensions forward from the previous build so we only ffprobe
        # each video once (not on every startup/sync rebuild). size_bytes rides along
        # as a change marker: it lets the upscaled-video branch below trust the cache
        # while the file on disk is unchanged.
        try:
            dim_cache = {
                r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                    "SELECT id, media_w, media_h, size_bytes FROM media "
                    "WHERE media_w IS NOT NULL AND media_h IS NOT NULL"
                )
            }
        except sqlite3.OperationalError:
            dim_cache = {}
        # End the implicit transaction the DDL above opened. The per-item loop below
        # does slow filesystem work (stats, thumbnail headers, the odd ffprobe) and
        # must NOT hold the write lock while it runs — that's minutes at library
        # scale, and it starved delete_media into "database is locked" failures.
        conn.commit()
        media_rows = []
        tag_rows = []
        fts_rows = []
        for item in items:
            mid = item["id"]
            rel = media_rel_path(item)
            href = _rel_href(rel, gallery_dir)
            # Thumbnails live sharded (thumbnails/<ab>/<id>.jpg); tolerate the legacy
            # flat path too so un-migrated libraries still show thumbnails.
            shard = media_shard(mid)
            thumb_rel = f"thumbnails/{shard}/{mid}.jpg"
            thumb_path = thumbnails_dir / shard / f"{mid}.jpg"
            if not thumb_path.exists():
                flat = thumbnails_dir / f"{mid}.jpg"
                if flat.exists():
                    thumb_path, thumb_rel = flat, f"thumbnails/{mid}.jpg"
            tw, th = _thumb_dims(thumb_path)
            media_file = gallery_dir / rel
            # File size is a cheap stat (no ffprobe), so capture it every build — it
            # doubles as the dim_cache's "has the file changed?" marker below.
            try:
                size_bytes = media_file.stat().st_size
            except OSError:
                size_bytes = None
            # Real source dimensions for the resolution badge: prefer values captured
            # from Grok at download time, else the previous build's cache, else probe
            # the file once (ffprobe/Pillow) and let the next build read it from cache.
            mw, mh = item.get("width"), item.get("height")
            src_name = str(item.get("source_url") or "").rsplit("?", 1)[0].rsplit("/", 1)[-1].lower()
            cached = dim_cache.get(mid)  # (w, h, size_bytes) from the previous build
            if item.get("media_type") == "video" and ("_hd" in src_name or "1080" in src_name):
                # Upscaled-in-place video: Grok's captured `resolution` is the BASE
                # generation size, and an upscale replaces the file under the SAME id —
                # so neither metadata nor a stale cache can be trusted blindly (a 1424²
                # upscale would wrongly badge as 544p). But an upscale always changes
                # the file's size, so the cache IS trustworthy while size_bytes still
                # matches the size recorded when the dims were cached. Only a changed
                # (or unknown) size re-probes: re-ffprobing every _hd video on every
                # rebuild cost ~50s per rebuild at 15k-library scale.
                if cached and size_bytes is not None and cached[2] == size_bytes:
                    mw, mh = cached[0], cached[1]
                else:
                    pw, ph = _video_dims(media_file)
                    if pw and ph:
                        mw, mh = pw, ph
                    elif not (mw and mh):
                        mw, mh = (cached[0], cached[1]) if cached else (mw, mh)
            elif not (mw and mh):
                mw, mh = ((cached[0], cached[1]) if cached
                          else _media_dims(item.get("media_type"), media_file))
            thumb_href = thumb_rel if thumb_path.exists() else None
            vtt = media_file.with_suffix(".vtt")
            subtitles = href.rsplit(".", 1)[0] + ".vtt" if vtt.exists() else None
            tags = tags_by_id.get(mid, [])
            key, _ = group_key_and_label(item)
            media_rows.append((
                mid, item.get("media_type"), item.get("prompt") or "", key,
                item.get("model"), item.get("created_at"), item.get("parent_id"),
                item.get("source_url"), rel, href, thumb_href, subtitles,
                item.get("canvas_id"), item.get("canvas_name"), tw, th, mw, mh,
                1 if subtitles else 0, size_bytes,
                1 if item.get("api_generated") else 0,
                item.get("preset"),
                *(None if item.get(col) is None else (1 if item.get(col) else 0)
                  for col in AUX_FLAG_COLUMNS),
            ))
            for tag in tags:
                tag_rows.append((mid, tag, "auto"))
            fts_rows.append([
                mid, item.get("prompt") or "", " ".join(tags),
                item.get("model") or "", rel.rsplit("/", 1)[-1],
            ])
        # Swap the tables in one SHORT write transaction (rows were prepared above
        # with no lock held): delete-all + bulk insert is a couple of seconds even
        # at library scale, so concurrent writers just wait it out via busy_timeout.
        # IMMEDIATE takes the write lock BEFORE tags.json is read: a concurrent
        # set_user_tags either committed already (and its tags.json write is visible
        # here) or waits for this swap and re-applies on top of it.
        conn.execute("BEGIN IMMEDIATE")
        user_tags = load_user_tag_items(
            user_tags_path if user_tags_path is not None else metadata_path.parent / USER_TAGS_FILE
        )
        indexed = {r[0] for r in fts_rows}
        for row in fts_rows:
            extra = user_tags.get(row[0])
            if extra:
                row[2] = " ".join([row[2], *extra]).strip()
        tag_rows.extend((mid, tag, "user") for mid, tl in user_tags.items() if mid in indexed for tag in tl)
        conn.execute("DELETE FROM media")
        conn.execute("DELETE FROM media_tags")
        conn.execute("DELETE FROM media_fts")
        conn.executemany(
            f"INSERT OR REPLACE INTO media ({','.join(MEDIA_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in MEDIA_COLUMNS)})",
            media_rows,
        )
        conn.executemany("INSERT OR IGNORE INTO media_tags (media_id, tag, source) VALUES (?, ?, ?)", tag_rows)
        conn.executemany(
            "INSERT INTO media_fts (id, prompt, tags, model, filename) VALUES (?, ?, ?, ?, ?)",
            fts_rows,
        )
        conn.commit()
        return len(media_rows)
    finally:
        conn.close()


def set_user_tags(db_path: str | Path, mapping: dict[str, list[str]]) -> int:
    """Mirror hand-assigned tags into the index for just the given media (``{id: tags}``;
    an empty list clears them) — no full rebuild. Refreshes each row's FTS ``tags`` text
    (prompt tags + user tags) so search finds them too. Ids not in the index are skipped
    (a later rebuild picks them up from tags.json). Returns rows updated."""
    mapping = {str(k): list(dict.fromkeys(str(t) for t in (v or []) if str(t).strip())) for k, v in mapping.items()}
    if not mapping:
        return 0
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_tag_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ids = list(mapping)
        present: set[str] = set()
        CHUNK = 400
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            present.update(r[0] for r in conn.execute(f"SELECT id FROM media WHERE id IN ({qmarks})", chunk))
            conn.execute(f"DELETE FROM media_tags WHERE source = 'user' AND media_id IN ({qmarks})", chunk)
        rows = [(mid, t, "user") for mid in ids if mid in present for t in mapping[mid]]
        conn.executemany("INSERT OR IGNORE INTO media_tags (media_id, tag, source) VALUES (?, ?, ?)", rows)
        present_list = [i for i in ids if i in present]
        for start in range(0, len(present_list), CHUNK):
            chunk = present_list[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            auto: dict[str, list[str]] = {}
            for mid, tag in conn.execute(
                f"SELECT media_id, tag FROM media_tags WHERE source = 'auto' AND media_id IN ({qmarks})", chunk
            ):
                auto.setdefault(mid, []).append(tag)
            fts = conn.execute(f"SELECT rowid, id FROM media_fts WHERE id IN ({qmarks})", chunk).fetchall()
            conn.executemany(
                "UPDATE media_fts SET tags = ? WHERE rowid = ?",
                [(" ".join([*auto.get(r[1], []), *mapping[r[1]]]), r[0]) for r in fts],
            )
        conn.commit()
        return len(present)
    finally:
        conn.close()


def _fts_query(raw: str) -> str:
    """Turn user text into a safe FTS5 prefix query: each term ANDed, prefix-matched."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in raw).split() if t]
    return " AND ".join(f'"{t}"*' for t in terms)


def _temp_id_table(conn: sqlite3.Connection, name: str, ids: Iterable[str]) -> bool:
    ids = list(ids)
    if not ids:
        return False
    conn.execute(f"CREATE TEMP TABLE {name} (id TEXT PRIMARY KEY)")
    conn.executemany(f"INSERT OR IGNORE INTO {name} (id) VALUES (?)", [(i,) for i in ids])
    return True


def query_media(
    db_path: str | Path,
    *,
    view: str = "files",
    q: str = "",
    tags: Iterable[str] = (),
    models: Iterable[str] = (),
    canvas: str | None = None,
    media_type: str = "all",
    resolutions: Iterable[int] = (),
    sort: str = "new",
    page: int = 1,
    page_size: int = 120,
    favorites: Iterable[str] = (),
    stashed: Iterable[str] = (),
    collection_ids: Iterable[str] = (),
    hidden: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
    tag_source: str = "all",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Paginated, filtered media query. ``favorites``/``stashed`` are id sets the
    caller supplies (from library.json); they drive Favorites/Archive and the
    global "hide archived from Recent" rule. ``all`` intentionally bypasses that
    hiding so users can find anything that still exists on disk. ``hidden`` is the
    media of locked collections — excluded from EVERY view (including ``all`` and
    search) until the collection is unlocked."""
    conn = _connect(db_path)
    try:
        has_stash = _temp_id_table(conn, "_stash", stashed)
        has_fav = _temp_id_table(conn, "_fav", favorites)
        has_collection = _temp_id_table(conn, "_collection", collection_ids)
        has_hidden = _temp_id_table(conn, "_hidden", hidden)

        where: list[str] = []
        params: list[Any] = []
        joins = ""

        if has_collection:
            where.append("m.id IN (SELECT id FROM _collection)")
        # Locked-collection media are hidden everywhere (no view opts out) until unlocked.
        if has_hidden:
            where.append("m.id NOT IN (SELECT id FROM _hidden)")

        if q.strip():
            match = _fts_query(q)
            if match:
                joins += " JOIN media_fts f ON f.id = m.id"
                where.append("media_fts MATCH ?")
                params.append(match)

        view = "recent" if view == "files" else ("archive" if view == "stashed" else view)
        if view == "archive":
            where.append("m.id IN (SELECT id FROM _stash)" if has_stash else "0")
        else:
            if has_stash and view not in ("all", "favorites", "canvases", "collections"):
                where.append("m.id NOT IN (SELECT id FROM _stash)")
            if view == "favorites":
                where.append("m.id IN (SELECT id FROM _fav)" if has_fav else "0")

        # Tag filter defaults to match-ANY (OR): a clip qualifies if it carries at least
        # one of the selected tags — mirrors the model filter and the Prompt Studio tag
        # cloud. ``tag_mode="all"`` requires every tag; ``tag_source`` limits the match to
        # prompt ('auto') or hand-assigned ('user') tags (default: either).
        tag_sql, tag_params = _tag_filter_clause(tags, tag_source, tag_mode)
        if tag_sql:
            where.append(tag_sql)
            params.extend(tag_params)

        model_list = list(models)
        if model_list:
            clauses = []
            for model in model_list:
                if model == "Unknown model":
                    clauses.append("m.model IS NULL OR m.model = ''")
                else:
                    clauses.append("m.model = ?")
                    params.append(model)
            where.append("(" + " OR ".join(clauses) + ")")

        if canvas:
            where.append("m.canvas_id = ?")
            params.append(canvas)
        # Resolution buckets are "<shorter-side>-<orientation>", e.g. "720-landscape".
        orient_sql = {
            "landscape": "m.media_w > m.media_h",
            "portrait": "m.media_w < m.media_h",
            "square": "m.media_w = m.media_h",
        }
        res_buckets = []
        for r in resolutions:
            h_str, sep, orient = str(r).strip().lower().rpartition("-")
            if sep and h_str.isdigit() and orient in orient_sql:
                res_buckets.append((int(h_str), orient))
        if res_buckets:
            # Match the shorter side AND orientation so each tier splits into its own
            # landscape / portrait / square bucket.
            clause = " OR ".join(
                f"(MIN(m.media_w, m.media_h) = ? AND {orient_sql[o]})" for _, o in res_buckets
            )
            where.append(f"({clause})")
            for h, _ in res_buckets:
                params.append(h)
        if media_type in ("image", "video"):
            where.append("m.media_type = ?")
            params.append(media_type)
        # Date bounds compare lexicographically against ISO created_at (works for
        # both "YYYY-MM-DD" and full "YYYY-MM-DDT..." timestamps).
        if start:
            where.append("m.created_at >= ?")
            params.append(start)
        if end:
            where.append("m.created_at < ?")
            params.append(end)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = {
            "old": "m.created_at ASC",
            "prompt": "m.prompt COLLATE NOCASE ASC",
            "model": "m.model COLLATE NOCASE ASC",
            # `m.size_bytes IS NULL` first keeps files whose size is unknown (e.g. the file
            # vanished off disk) at the bottom in BOTH directions; created_at breaks ties.
            "size": "m.size_bytes IS NULL, m.size_bytes DESC, m.created_at DESC",
            "size_asc": "m.size_bytes IS NULL, m.size_bytes ASC, m.created_at DESC",
        }.get(sort, "m.created_at DESC")

        total = conn.execute(f"SELECT COUNT(*) FROM media m{joins}{where_sql}", params).fetchone()[0]
        page = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
        rows = conn.execute(
            f"SELECT m.* FROM media m{joins}{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            d = _attach_tags(conn, dict(row))
            d["has_subtitles"] = bool(d["has_subtitles"])
            d["api_generated"] = bool(d.get("api_generated"))
            _decode_flags(d)
            items.append(d)
        return {"total": total, "page": page, "page_size": page_size, "items": items}
    finally:
        conn.close()


def media_by_ids(db_path: str | Path, ids: list[str]) -> list[dict[str, Any]]:
    """Resolve media rows for an explicit id list, preserving the given order
    (used to play/reorder/export a playlist)."""
    ids = [str(i) for i in ids]
    if not ids:
        return []
    conn = _connect(db_path)
    try:
        by_id: dict[str, dict] = {}
        CHUNK = 400
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT * FROM media WHERE id IN ({qmarks})", chunk):
                d = dict(row)
                d["has_subtitles"] = bool(d["has_subtitles"])
                d["api_generated"] = bool(d.get("api_generated"))
                _decode_flags(d)
                _attach_tags(conn, d)
                by_id[d["id"]] = d
        return [by_id[i] for i in ids if i in by_id]
    finally:
        conn.close()


_ORIENT_SQL = ("CASE WHEN media_w > media_h THEN 'landscape' "
               "WHEN media_w < media_h THEN 'portrait' ELSE 'square' END")
# Source videos only: montage outputs are renders, not montage material.
_SOURCE_VIDEO_SQL = "media_type = 'video' AND (model IS NULL OR model != 'Beat Montage')"


def video_resolution_stats(db_path: str | Path, ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Exact-dimension histogram of source videos (montage outputs excluded):
    ``[{w, h, orientation, count}]``, biggest count first. ``ids=None`` covers the
    whole library; a list restricts to those ids. Rows without captured dimensions
    (an index predating dimension capture) come back as one ``w=h=None`` bucket so
    callers can surface "unknown" honestly instead of silently dropping them.
    Drives the Generate Movie panel's aspect/resolution picker."""
    conn = _connect(db_path)
    try:
        counts: dict[tuple, int] = {}
        select = (f"SELECT media_w AS w, media_h AS h, {_ORIENT_SQL} AS orient, "
                  f"COUNT(*) AS n FROM media WHERE {_SOURCE_VIDEO_SQL}")
        if ids is None:
            for r in conn.execute(select + " GROUP BY w, h"):
                key = (r["w"], r["h"], r["orient"] if r["w"] is not None else None)
                counts[key] = counts.get(key, 0) + r["n"]
        else:
            CHUNK = 400
            uniq = list(dict.fromkeys(str(i) for i in ids))
            for start in range(0, len(uniq), CHUNK):
                chunk = uniq[start:start + CHUNK]
                qmarks = ",".join("?" for _ in chunk)
                for r in conn.execute(select + f" AND id IN ({qmarks}) GROUP BY w, h", chunk):
                    key = (r["w"], r["h"], r["orient"] if r["w"] is not None else None)
                    counts[key] = counts.get(key, 0) + r["n"]
        return [{"w": w, "h": h, "orientation": o, "count": n}
                for (w, h, o), n in sorted(counts.items(),
                                           key=lambda kv: (-kv[1], str(kv[0])))]
    finally:
        conn.close()


def video_orientation_map(db_path: str | Path) -> dict[str, str]:
    """``{media_id: orientation}`` for every dimensioned source video — one
    query, so callers can count pool eligibility per collection without a query
    per collection (the montage panel's aspect-scoped collection chips)."""
    conn = _connect(db_path)
    try:
        return {r["id"]: r["orient"] for r in conn.execute(
            f"SELECT id, {_ORIENT_SQL} AS orient FROM media "
            f"WHERE {_SOURCE_VIDEO_SQL} AND media_w IS NOT NULL AND media_h IS NOT NULL")}
    finally:
        conn.close()


def filter_video_ids_by_orientation(db_path: str | Path, ids: list[str] | None,
                                    orientation: str) -> list[str]:
    """The subset of ``ids`` (order preserved) whose video dimensions match
    ``orientation`` — 'landscape' | 'portrait' | 'square'. ``ids=None`` returns
    every matching source-video id in the library. Rows without captured
    dimensions never match (their shape can't be verified), so an aspect-filtered
    montage never admits a clip that might render cropped. Montage outputs are
    always excluded."""
    if orientation not in ("landscape", "portrait", "square"):
        return [str(i) for i in (ids or [])]
    conn = _connect(db_path)
    try:
        base = (f"SELECT id FROM media WHERE {_SOURCE_VIDEO_SQL} "
                f"AND media_w IS NOT NULL AND media_h IS NOT NULL "
                f"AND {_ORIENT_SQL} = ?")
        if ids is None:
            return [r["id"] for r in conn.execute(base, (orientation,))]
        keep: set[str] = set()
        uniq = list(dict.fromkeys(str(i) for i in ids))
        CHUNK = 400
        for start in range(0, len(uniq), CHUNK):
            chunk = uniq[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            keep.update(r["id"] for r in conn.execute(
                base + f" AND id IN ({qmarks})", (orientation, *chunk)))
        return [i for i in (str(x) for x in ids) if i in keep]
    finally:
        conn.close()


def _decode_flags(d: dict[str, Any]) -> dict[str, Any]:
    """1/0/None -> True/False/None. bool() would fold None into False and so claim Grok
    rated everything archived before these flags existed; the None passes through."""
    for col in AUX_FLAG_COLUMNS:
        if d.get(col) is not None:
            d[col] = bool(d[col])
    return d


def _media_dict(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["has_subtitles"] = bool(d["has_subtitles"])
    d["api_generated"] = bool(d.get("api_generated"))
    _decode_flags(d)
    return _attach_tags(conn, d)


def media_related(db_path: str | Path, media_id: str, limit: int = 100) -> dict[str, Any]:
    """Local media related through downloader parent_id links.

    Videos generated from a base image store parent_id=<image id>. Return the
    local base image for a video, and local generated videos for an image.
    """
    conn = _connect(db_path)
    try:
        current = _media_dict(conn, conn.execute("SELECT * FROM media WHERE id = ?", (str(media_id),)).fetchone())
        if not current:
            return {"base": None, "generated": []}

        base = None
        if current.get("media_type") == "video" and current.get("parent_id"):
            base = _media_dict(
                conn,
                conn.execute("SELECT * FROM media WHERE id = ?", (str(current["parent_id"]),)).fetchone(),
            )

        generated: list[dict[str, Any]] = []
        if current.get("media_type") == "image":
            rows = conn.execute(
                "SELECT * FROM media WHERE parent_id = ? AND media_type = 'video' "
                "ORDER BY created_at DESC LIMIT ?",
                (str(media_id), max(1, min(500, int(limit)))),
            ).fetchall()
            generated = [d for row in rows if (d := _media_dict(conn, row))]

        return {"base": base, "generated": generated}
    finally:
        conn.close()


def delete_media(db_path: str | Path, ids: list[str]) -> int:
    """Remove media rows (and their tags / FTS entries) for the given ids. Returns
    the number of media rows deleted. The DB is derived, so this just keeps the
    read-model in sync after a hard delete without a full rebuild."""
    ids = [str(i) for i in ids]
    if not ids:
        return 0
    conn = _connect(db_path)
    try:
        deleted = 0
        CHUNK = 400
        for start in range(0, len(ids), CHUNK):
            chunk = ids[start:start + CHUNK]
            qmarks = ",".join("?" for _ in chunk)
            deleted += conn.execute(f"DELETE FROM media WHERE id IN ({qmarks})", chunk).rowcount
            conn.execute(f"DELETE FROM media_tags WHERE media_id IN ({qmarks})", chunk)
            conn.execute(f"DELETE FROM media_fts WHERE id IN ({qmarks})", chunk)
        conn.commit()
        return deleted
    finally:
        conn.close()


def facets(
    db_path: str | Path,
    *,
    view: str = "recent",
    q: str = "",
    tags: Iterable[str] = (),
    models: Iterable[str] = (),
    resolutions: Iterable[int] = (),
    canvas: str | None = None,
    media_type: str = "all",
    favorites: Iterable[str] = (),
    stashed: Iterable[str] = (),
    collection_ids: Iterable[str] = (),
    hidden: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
    tag_source: str = "all",
    tag_mode: str = "any",
) -> dict[str, Any]:
    """Tag / model / canvas / resolution counts for the current browsing scope.

    Cross-facet: each facet's counts reflect the OTHER active chip selections but
    exclude its own dimension — so selecting tags narrows the resolution and model
    counts (and vice versa), while each facet still lists all of its own options so
    you can keep multi-selecting within it. ``hidden`` (locked-collection media) is
    excluded so counts never leak them."""
    conn = _connect(db_path)
    try:
        has_stash = _temp_id_table(conn, "_stash", stashed)
        has_fav = _temp_id_table(conn, "_fav", favorites)
        has_collection = _temp_id_table(conn, "_collection", collection_ids)
        has_hidden = _temp_id_table(conn, "_hidden", hidden)

        where: list[str] = []
        params: list[Any] = []
        joins = ""

        if has_hidden:
            where.append("m.id NOT IN (SELECT id FROM _hidden)")

        if q.strip():
            match = _fts_query(q)
            if match:
                joins += " JOIN media_fts f ON f.id = m.id"
                where.append("media_fts MATCH ?")
                params.append(match)

        view = "recent" if view == "files" else ("archive" if view == "stashed" else view)
        if has_collection:
            where.append("m.id IN (SELECT id FROM _collection)")
        elif view == "archive":
            where.append("m.id IN (SELECT id FROM _stash)" if has_stash else "0")
        else:
            if has_stash and view not in ("all", "favorites", "canvases", "collections"):
                where.append("m.id NOT IN (SELECT id FROM _stash)")
            if view == "favorites":
                where.append("m.id IN (SELECT id FROM _fav)" if has_fav else "0")

        if canvas:
            where.append("m.canvas_id = ?")
            params.append(canvas)
        if media_type in ("image", "video"):
            where.append("m.media_type = ?")
            params.append(media_type)
        if start:
            where.append("m.created_at >= ?")
            params.append(start)
        if end:
            where.append("m.created_at < ?")
            params.append(end)

        # Per-dimension chip clauses (match-ANY within a dimension), each as (sql, params).
        # A facet applies every OTHER dimension's clause but not its own, so selecting
        # tags narrows the resolution/model counts while the tag list still shows every
        # tag (letting you keep adding to the OR). `where`/`params` above are the base scope.
        orient_sql = {
            "landscape": "m.media_w > m.media_h",
            "portrait": "m.media_w < m.media_h",
            "square": "m.media_w = m.media_h",
        }

        def tag_clause() -> tuple[str | None, list[Any]]:
            return _tag_filter_clause(tags, tag_source, tag_mode, alias="mt2")

        def model_clause() -> tuple[str | None, list[Any]]:
            ml = [m for m in models if m]
            if not ml:
                return None, []
            clauses: list[str] = []
            ps: list[Any] = []
            for model in ml:
                if model == "Unknown model":
                    clauses.append("(m.model IS NULL OR m.model = '')")
                else:
                    clauses.append("m.model = ?")
                    ps.append(model)
            return "(" + " OR ".join(clauses) + ")", ps

        def res_clause() -> tuple[str | None, list[Any]]:
            buckets: list[tuple[int, str]] = []
            for r in resolutions:
                h_str, sep, orient = str(r).strip().lower().rpartition("-")
                if sep and h_str.isdigit() and orient in orient_sql:
                    buckets.append((int(h_str), orient))
            if not buckets:
                return None, []
            clause = " OR ".join(
                f"(MIN(m.media_w, m.media_h) = ? AND {orient_sql[o]})" for _, o in buckets
            )
            return f"({clause})", [h for h, _ in buckets]

        def compose(*extra: tuple[str | None, list[Any]]) -> tuple[str, list[Any]]:
            w = where[:]
            p = params[:]
            for sql, ps in extra:
                if sql:
                    w.append(sql)
                    p.extend(ps)
            return ((" WHERE " + " AND ".join(w)) if w else ""), p

        tags_where, tags_params = compose(model_clause(), res_clause())

        def source_tag_rows(source: str) -> list[dict[str, Any]]:
            src_where = (tags_where + " AND" if tags_where else " WHERE") + " mt.source = ?"
            return [
                {"name": r["tag"], "count": r["n"]}
                for r in conn.execute(
                    f"SELECT mt.tag, COUNT(*) n FROM media_tags mt JOIN media m ON m.id = mt.media_id{joins}{src_where} "
                    f"GROUP BY mt.tag ORDER BY n DESC, mt.tag",
                    [*tags_params, source],
                )
            ]

        # `tags` keeps its meaning (prompt-derived); hand-assigned tags count separately.
        tag_rows = source_tag_rows("auto")
        user_tag_rows = source_tag_rows("user")
        models_where, models_params = compose(tag_clause(), res_clause())
        model_rows = [
            {"name": r["model"] or "Unknown model", "count": r["n"]}
            for r in conn.execute(
                f"SELECT COALESCE(NULLIF(m.model,''), NULL) model, COUNT(*) n FROM media m{joins}{models_where} "
                f"GROUP BY model ORDER BY n DESC",
                models_params,
            )
        ]
        canvas_where, canvas_params = compose(tag_clause(), model_clause(), res_clause())
        # created_at/updated_at are derived from the canvas's own media (a canvas has no
        # row of its own): oldest item = when it started, newest = when it last grew.
        # The Canvases landing sorts on them (Recent / Recently updated).
        canvas_rows = [
            {"id": r["canvas_id"], "name": r["canvas_name"] or r["canvas_id"],
             "count": r["n"], "videos": r["v"], "images": r["i"], "cover": r["cover"],
             # The thumb path is thumbnails/<shard>/<id>.jpg, so the basename IS the
             # media id — surfaced so the client can srcset the /covers high-res tier.
             "cover_id": (r["cover"] or "").rsplit("/", 1)[-1].rsplit(".", 1)[0] or None,
             "created_at": r["first_at"], "updated_at": r["last_at"]}
            for r in conn.execute(
                f"SELECT m.canvas_id, m.canvas_name, COUNT(*) n, "
                f"SUM(m.media_type='video') v, SUM(m.media_type='image') i, "
                f"MIN(m.created_at) first_at, MAX(m.created_at) last_at, "
                f"MAX(m.thumb) cover FROM media m{joins}{canvas_where}"
                f"{' AND' if canvas_where else ' WHERE'} m.canvas_id IS NOT NULL "
                f"GROUP BY m.canvas_id ORDER BY n DESC",
                canvas_params,
            )
        ]
        res_base_where, res_params = compose(tag_clause(), model_clause())
        res_where = (res_base_where + " AND" if res_base_where else " WHERE") + \
            " m.media_w IS NOT NULL AND m.media_h IS NOT NULL"
        resolution_rows = [
            {"height": r["h"], "orientation": r["orient"], "count": r["n"]}
            for r in conn.execute(
                f"SELECT MIN(m.media_w, m.media_h) AS h, "
                f"CASE WHEN m.media_w > m.media_h THEN 'landscape' "
                f"WHEN m.media_w < m.media_h THEN 'portrait' ELSE 'square' END AS orient, "
                f"COUNT(*) n FROM media m{joins}{res_where} "
                f"GROUP BY h, orient ORDER BY h DESC, orient",
                res_params,
            )
        ]
        # Total stays the scope count (ignores chips) — its prior meaning; unused by the UI.
        total = conn.execute(f"SELECT COUNT(*) FROM media m{joins}"
                             f"{(' WHERE ' + ' AND '.join(where)) if where else ''}", params).fetchone()[0]
        return {"tags": tag_rows, "user_tags": user_tag_rows, "models": model_rows, "canvases": canvas_rows,
                "resolutions": resolution_rows, "total": total}
    finally:
        conn.close()


def _created_bound(moment: datetime) -> str:
    """A UTC instant rendered as a lexicographic lower bound for ``created_at``.

    ``created_at`` is stored as ISO-8601 UTC with a ``Z`` suffix and (usually)
    microseconds — ``2026-06-21T03:38:31.635443Z`` — so a string ``>=`` is a valid
    time comparison. The ``Z`` is deliberately NOT emitted: ``.`` sorts before
    ``Z``, so a bound of ``…T05:00:00Z`` would exclude a row stamped
    ``…T05:00:00.123456Z`` — an item generated in the same second as the boundary
    would vanish from the window.

    Midnight collapses to the DATE alone, which additionally keeps a hypothetical
    legacy date-only row (``2026-06-21``) inside its own day rather than sorting it
    just below the padded ``…T00:00:00``. That only helps a viewer at UTC, since
    any other offset puts the day boundary at a non-midnight UTC instant and a bare
    date then genuinely resolves to the previous local day. No such rows exist in
    practice (every indexed timestamp carries a time)."""
    if (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def stats(db_path: str | Path, tz_offset_minutes: int = 0) -> dict[str, Any]:
    """Whole-library totals for the Stats panel: media counts by type and the
    summed on-disk size. ``size_bytes`` is captured at index time, so this is a
    single cheap aggregate query — no filesystem walk. Rows with a missing size
    (older indexes) just contribute 0 to the byte total.

    ``today`` carries the counts and bytes added since local midnight; ``month``
    the current-month creation counts plus the days elapsed so far, from which the
    UI derives per-day averages.

    ``tz_offset_minutes`` is the VIEWER's offset east of UTC (JS
    ``-new Date().getTimezoneOffset()``), because "today" has to mean the day the
    person reading the panel is living in. The container has no ``TZ`` set, so the
    server's own clock is UTC — deriving the day from it would roll the counter
    over mid-evening for anyone west of Greenwich. Both windows use the same
    offset so the day counter and the per-day average never disagree about the
    date. Rows with a NULL ``created_at`` (older indexes) fall out of both."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(media_type='video'), 0) AS videos, "
            "COALESCE(SUM(media_type='image'), 0) AS images, "
            "COUNT(*) AS total, "
            "COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM media"
        ).fetchone()
        offset = timedelta(minutes=tz_offset_minutes)
        local_now = datetime.now(timezone.utc) + offset
        # Local midnight / month-start, converted back to the UTC instant the stored
        # timestamps are expressed in.
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - offset
        month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - offset
        window = conn.execute(
            "SELECT "
            "COALESCE(SUM(media_type='video'), 0) AS videos, "
            "COALESCE(SUM(media_type='image'), 0) AS images, "
            "COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM media WHERE created_at >= ?",
            (_created_bound(day_start),),
        ).fetchone()
        month = conn.execute(
            "SELECT "
            "COALESCE(SUM(media_type='video'), 0) AS videos, "
            "COALESCE(SUM(media_type='image'), 0) AS images "
            "FROM media WHERE created_at >= ?",
            (_created_bound(month_start),),
        ).fetchone()
        return {
            "videos": row["videos"],
            "images": row["images"],
            "total": row["total"],
            "bytes": row["bytes"],
            "today": {
                "videos": window["videos"],
                "images": window["images"],
                "bytes": window["bytes"],
                "date": local_now.strftime("%Y-%m-%d"),
            },
            "month": {"videos": month["videos"], "images": month["images"], "days": local_now.day},
        }
    finally:
        conn.close()
