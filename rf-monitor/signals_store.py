"""RF Monitor v2 - signal episode store.

One row per *signal episode*: a blob of adjacent 1 MHz sweep bins that rose above
its own noise floor. Episodes live in SQLite rather than InfluxDB because

  * the rf_sweep bucket has a short retention window, and the dashboard needs
    week-over-week novelty ("have I heard this before?"),
  * the dashboard needs relational filters (type, band, time range, min SNR),
  * operator labels (known / ignore / notable) have no home in a time series.

Artifacts (spectrogram PNG, WAV, decoded text/image, raw IQ) live on disk under
SIGNALS_DIR/artifacts/<episode_id>/ and are tracked here so the 1 GB cap can be
enforced by evicting the oldest files while keeping the episode row.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/New_York")
SIGNALS_DIR = Path(os.environ.get("RF_SIGNALS_DIR", "/home/cmilkosk/rf-monitor/signals"))
DB_PATH = Path(os.environ.get("RF_SIGNALS_DB", str(SIGNALS_DIR / "signals.db")))
ARTIFACT_DIR = Path(os.environ.get("RF_ARTIFACT_DIR", str(SIGNALS_DIR / "artifacts")))
CAP_BYTES = int(os.environ.get("RF_ARTIFACT_CAP_BYTES", str(1024 ** 3)))

DECODE_TYPES = ("voice", "music", "ism", "aircraft", "aprs", "pager", "satellite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    freq_hz         INTEGER NOT NULL,
    low_hz          INTEGER NOT NULL,
    high_hz         INTEGER NOT NULL,
    bandwidth_hz    INTEGER NOT NULL,
    n_bins          INTEGER NOT NULL DEFAULT 1,
    peak_snr_db     REAL NOT NULL,
    mean_snr_db     REAL NOT NULL,
    delta_db        REAL,
    peak_rssi_db    REAL,
    floor_db        REAL,
    duty            REAL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    sightings       INTEGER NOT NULL DEFAULT 1,
    state           TEXT NOT NULL DEFAULT 'open',
    class           TEXT NOT NULL DEFAULT 'unexplained',
    band            TEXT,
    likely          TEXT,
    confidence      TEXT,
    score           REAL NOT NULL DEFAULT 0,
    is_new          INTEGER NOT NULL DEFAULT 1,
    known_emitter   TEXT,
    decode_type     TEXT,
    decode_status   TEXT NOT NULL DEFAULT 'none',
    decoded_summary TEXT,
    capture_id      TEXT,
    features_json   TEXT,
    intel_json      TEXT,
    source          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ep_last   ON episodes(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_ep_first  ON episodes(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_ep_freq   ON episodes(freq_hz);
CREATE INDEX IF NOT EXISTS idx_ep_dtype  ON episodes(decode_type);
CREATE INDEX IF NOT EXISTS idx_ep_state  ON episodes(state);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    purged      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_art_ep ON artifacts(episode_id);

CREATE TABLE IF NOT EXISTS labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL,
    label       TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_emitters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    freq_low_hz  INTEGER NOT NULL,
    freq_high_hz INTEGER NOT NULL,
    name         TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decode_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL,
    decoder     TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_ep ON decode_attempts(episode_id);
"""


# --------------------------------------------------------------------------- #
# time helpers - everything is stored as UTC "YYYY-MM-DDTHH:MM:SSZ" so plain
# lexicographic comparison in SQL is also chronological comparison.
# --------------------------------------------------------------------------- #
def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def local_day_start_utc(days_ago: int = 0) -> str:
    """UTC timestamp for 00:00 America/New_York, `days_ago` days back."""
    now_local = datetime.now(LOCAL_TZ)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return to_iso(start)


def local_iso(value: str) -> str:
    """Render a stored UTC stamp in the operator's timezone."""
    try:
        return parse_iso(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return value


def local_hm(value: str) -> str:
    try:
        return parse_iso(value).astimezone(LOCAL_TZ).strftime("%H:%M")
    except Exception:
        return value


# --------------------------------------------------------------------------- #
# connection / schema
# --------------------------------------------------------------------------- #
def connect() -> sqlite3.Connection:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_db() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        # additive migrations for DBs created by an earlier v2 build
        columns = {row[1] for row in conn.execute("PRAGMA table_info(episodes)")}
        for name, ddl in (("delta_db", "ALTER TABLE episodes ADD COLUMN delta_db REAL"),):
            if name not in columns:
                conn.execute(ddl)
        conn.commit()


# --------------------------------------------------------------------------- #
# episode writes
# --------------------------------------------------------------------------- #
def episode_artifact_dir(episode_id: int) -> Path:
    path = ARTIFACT_DIR / str(episode_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def seen_before(freq_hz: int, tolerance_hz: int, before_iso: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM episodes WHERE ABS(freq_hz - ?) <= ? AND first_seen < ? LIMIT 1",
            (int(freq_hz), int(tolerance_hz), before_iso),
        ).fetchone()
    return row is not None


REOPEN_HOURS = float(os.environ.get("RF_SIGNALS_REOPEN_HOURS", "6"))


def _best_overlap(rows: list[sqlite3.Row], low_hz: int, high_hz: int,
                  min_overlap: float, width_ratio: float = 0.4) -> sqlite3.Row | None:
    best: sqlite3.Row | None = None
    best_fraction = 0.0
    for row in rows:
        overlap = min(int(high_hz), int(row["high_hz"])) - max(int(low_hz), int(row["low_hz"]))
        if overlap <= 0:
            continue
        smaller = min(int(high_hz) - int(low_hz), int(row["high_hz"]) - int(row["low_hz"]))
        larger = max(int(high_hz) - int(low_hz), int(row["high_hz"]) - int(row["low_hz"]))
        fraction = overlap / max(1, smaller)
        if fraction < min_overlap:
            continue
        # Comparable widths only. A 1 MHz signal sitting inside a 30 MHz band scores 100%
        # overlap on the smaller span and used to be absorbed into the band's row, which is
        # how seven rows came to span 18-113 MHz. Same emitter means similar footprint.
        if smaller / max(1, larger) < width_ratio:
            continue
        if fraction > best_fraction:
            best_fraction, best = fraction, row
    return best if best_fraction >= min_overlap else None


def match_episode(
    low_hz: int, high_hz: int, tolerance_hz: int = 1_500_000, min_overlap: float = 0.25,
    *, open_only: bool = True, since_iso: str | None = None,
) -> sqlite3.Row | None:
    sql = "SELECT * FROM episodes WHERE high_hz >= ? AND low_hz <= ?"
    params: list[Any] = [int(low_hz) - int(tolerance_hz), int(high_hz) + int(tolerance_hz)]
    if open_only:
        sql += " AND state='open'"
    if since_iso:
        sql += " AND last_seen >= ?"
        params.append(since_iso)
    sql += " ORDER BY last_seen DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return _best_overlap(rows, low_hz, high_hz, min_overlap)


def match_open_episode(
    low_hz: int, high_hz: int, tolerance_hz: int = 1_500_000, min_overlap: float = 0.25
) -> sqlite3.Row | None:
    """Match on spectral overlap, not centre distance.

    A wide emitter's estimated centre wanders a few MHz between scans (Wi-Fi at
    2459 vs 2461 MHz), so a centre-distance test spawns a duplicate row every few
    minutes. Overlap matching keeps one row per real emitter.
    """
    return match_episode(low_hz, high_hz, tolerance_hz, min_overlap, open_only=True)


def match_recent_episode(
    low_hz: int, high_hz: int, tolerance_hz: int = 1_500_000, min_overlap: float = 0.25,
    hours: float = REOPEN_HOURS,
) -> sqlite3.Row | None:
    """Match an episode seen within `hours`, whether it is open or closed.

    Why: close_stale() closes an episode after 15 min without a sighting, and the insert
    path then made a NEW row when the emitter returned. That - not transient blips - is
    where ~1,872 episodes/day came from while the journal showed new=0 updated=8 per
    cycle. Reopening the recent row keeps one row per emitter per day instead of one per
    15-minute window.
    """
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(hours=hours))
    return match_episode(low_hz, high_hz, tolerance_hz, min_overlap,
                         open_only=False, since_iso=cutoff)


def upsert_blob(blob: dict[str, Any], tolerance_hz: int, stale_minutes: int = 15) -> dict[str, Any]:
    """Create or refresh the episode for a detected blob. Returns a small result dict."""
    now = utcnow()
    match = match_open_episode(blob["low_hz"], blob["high_hz"], tolerance_hz)
    if match is None:
        # Reopen a recently-seen episode instead of minting a duplicate row after the
        # 15-minute stale close.
        match = match_recent_episode(blob["low_hz"], blob["high_hz"], tolerance_hz)
    if match is not None:
        episode_id = int(match["id"])
        peak_snr = max(float(match["peak_snr_db"]), float(blob["peak_snr_db"]))
        first = match["first_seen"]
        with connect() as conn:
            conn.execute(
                # Span follows the CURRENT blob. It used to be
                # low_hz=MIN(low_hz,?), high_hz=MAX(high_hz,?), which only ever grew: episode 1
                # reached 18.0-72.1 MHz (54.1 MHz) while its widest single blob was 48 bins
                # (24 MHz). Over 14,431 refreshes a row became a union of everything nearby
                # instead of one emitter's footprint.
                "UPDATE episodes SET freq_hz=?, low_hz=?, high_hz=?, bandwidth_hz=?,"
                " n_bins=?,"
                " peak_snr_db=?, mean_snr_db=?, delta_db=?, peak_rssi_db=?, floor_db=?, duty=?,"
                " last_seen=?, sightings=sightings+1, state='open', score=?,"
                " band=?, likely=?, confidence=?, class=?, known_emitter=?, features_json=?"
                " WHERE id=?",
                (
                    int(blob["freq_hz"]), int(blob["low_hz"]), int(blob["high_hz"]),
                    int(blob["bandwidth_hz"]), int(blob["n_bins"]),
                    peak_snr, float(blob["mean_snr_db"]), float(blob.get("delta_db") or 0.0),
                    float(blob["peak_rssi_db"]),
                    float(blob["floor_db"]), float(blob["duty"]),
                    now, float(blob["score"]),
                    blob.get("band"), blob.get("likely"), blob.get("confidence"),
                    blob.get("class", "unexplained"), blob.get("known_emitter"),
                    blob.get("features_json"),
                    episode_id,
                ),
            )
            conn.commit()
        return {"id": episode_id, "created": False, "first_seen": first}

    is_new = 0 if seen_before(blob["freq_hz"], tolerance_hz, local_day_start_utc(1)) else 1
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO episodes (freq_hz, low_hz, high_hz, bandwidth_hz, n_bins, peak_snr_db,"
            " mean_snr_db, delta_db, peak_rssi_db, floor_db, duty, first_seen, last_seen, sightings,"
            " state, class, band, likely, confidence, score, is_new, known_emitter, decode_status,"
            " features_json, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,'open',?,?,?,?,?,?,?,'none',?,?,?)",
            (
                int(blob["freq_hz"]), int(blob["low_hz"]), int(blob["high_hz"]),
                int(blob["bandwidth_hz"]), int(blob["n_bins"]),
                float(blob["peak_snr_db"]), float(blob["mean_snr_db"]),
                float(blob.get("delta_db") or 0.0),
                float(blob["peak_rssi_db"]), float(blob["floor_db"]), float(blob["duty"]),
                now, now,
                blob.get("class", "unexplained"), blob.get("band"),
                blob.get("likely"), blob.get("confidence"), float(blob["score"]),
                is_new, blob.get("known_emitter"),
                blob.get("features_json"), blob.get("source", "hackrf_sweep"), now,
            ),
        )
        conn.commit()
        episode_id = int(cur.lastrowid or 0)
    return {"id": episode_id, "created": True, "is_new": bool(is_new)}


def close_stale(stale_minutes: int = 15) -> int:
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(minutes=stale_minutes))
    with connect() as conn:
        cur = conn.execute(
            "UPDATE episodes SET state='closed' WHERE state='open' AND last_seen < ?", (cutoff,)
        )
        conn.commit()
        return cur.rowcount


def mark_decoded(episode_id: int, decode_type: str, summary: str, capture_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE episodes SET decode_type=?, decode_status='decoded', decoded_summary=?,"
            " capture_id=COALESCE(?, capture_id) WHERE id=?",
            (decode_type, summary, capture_id, int(episode_id)),
        )
        conn.commit()


def set_decode_status(episode_id: int, status: str, detail: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE episodes SET decode_status=? WHERE id=?", (status, int(episode_id))
        )
        if detail:
            conn.execute(
                "INSERT INTO decode_attempts (episode_id, decoder, status, detail, started_at)"
                " VALUES (?,?,?,?,?)",
                (int(episode_id), status, status, detail, utcnow()),
            )
        conn.commit()


def record_attempt(episode_id: int, decoder: str, status: str, detail: str | None,
                   started_at: str | None = None) -> None:
    """Log one decoder attempt. `status` is ok / no_frame / error / skipped."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO decode_attempts (episode_id, decoder, status, detail, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?)",
            (int(episode_id), str(decoder), str(status), (detail or "")[:500],
             started_at or utcnow(), utcnow()),
        )
        conn.commit()


def set_intel(episode_id: int, intel: dict[str, Any]) -> None:
    import json

    with connect() as conn:
        conn.execute(
            "UPDATE episodes SET intel_json=?, likely=COALESCE(?, likely),"
            " confidence=COALESCE(?, confidence), band=COALESCE(?, band) WHERE id=?",
            (
                json.dumps(intel),
                intel.get("likely"),
                intel.get("confidence"),
                ", ".join(b["name"] for b in intel.get("bands", [])) or None,
                int(episode_id),
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# artifacts + retention
# --------------------------------------------------------------------------- #
def add_artifact(episode_id: int, kind: str, path: Path | str, size: int | None = None) -> None:
    p = Path(path)
    if size is None:
        size = p.stat().st_size if p.exists() else 0
    with connect() as conn:
        conn.execute(
            "INSERT INTO artifacts (episode_id, kind, path, bytes, created_at) VALUES (?,?,?,?,?)",
            (int(episode_id), kind, str(p), int(size), utcnow()),
        )
        conn.commit()


def artifact_bytes() -> int:
    total = 0
    if not ARTIFACT_DIR.exists():
        return 0
    for root, _dirs, files in os.walk(ARTIFACT_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def enforce_cap(cap_bytes: int = CAP_BYTES) -> dict[str, int]:
    """Evict artifacts oldest-first until under the cap. Episode rows survive.

    Raw IQ goes first (it is the biggest and least useful once features and the
    spectrogram exist), then whole artifact directories oldest-first.
    """
    used = artifact_bytes()
    removed = 0
    freed = 0
    if used <= cap_bytes:
        return {"bytes": used, "removed": 0, "freed": 0, "under_cap": 1}

    # 1) drop IQ files first, oldest episode first
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.path, a.bytes FROM artifacts a JOIN episodes e ON e.id = a.episode_id"
            " WHERE a.purged=0 AND a.kind='iq' ORDER BY e.first_seen ASC"
        ).fetchall()
        for row in rows:
            if used <= cap_bytes:
                break
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("UPDATE artifacts SET purged=1 WHERE id=?", (row["id"],))
            used -= int(row["bytes"])
            freed += int(row["bytes"])
            removed += 1
        conn.commit()

    # 2) then whole episode directories, oldest first
    if used > cap_bytes:
        with connect() as conn:
            episodes = conn.execute(
                "SELECT id FROM episodes ORDER BY first_seen ASC"
            ).fetchall()
            for ep in episodes:
                if used <= cap_bytes:
                    break
                directory = ARTIFACT_DIR / str(ep["id"])
                if not directory.exists():
                    continue
                size = 0
                for root, _dirs, files in os.walk(directory):
                    for name in files:
                        try:
                            size += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
                for root, dirs, files in os.walk(directory, topdown=False):
                    for name in files:
                        try:
                            os.unlink(os.path.join(root, name))
                        except OSError:
                            pass
                    for name in dirs:
                        try:
                            os.rmdir(os.path.join(root, name))
                        except OSError:
                            pass
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
                conn.execute("UPDATE artifacts SET purged=1 WHERE episode_id=?", (ep["id"],))
                used -= size
                freed += size
                removed += 1
            conn.commit()

    return {"bytes": max(0, used), "removed": removed, "freed": freed, "under_cap": int(used <= cap_bytes)}


# --------------------------------------------------------------------------- #
# reads for the dashboard
# --------------------------------------------------------------------------- #
SORTABLE = {
    "time": "last_seen",
    "first_seen": "first_seen",
    "freq": "freq_hz",
    "snr": "peak_snr_db",
    "score": "score",
    "duration": "(julianday(last_seen) - julianday(first_seen))",
    "sightings": "sightings",
}

# A contiguous blob cannot be wider than its own bin count. Rows built by the old
# MIN/MAX span accumulation violate this (episode 1 claimed 54.1 MHz from 48 bins), so
# they are hidden from the list rather than deleted - the history stays, the display stops
# asserting something physically impossible.
SPAN_INVARIANT = "bandwidth_hz <= n_bins * 1000000 + 1000000"


def query_episodes(
    *,
    section: str = "latest",
    decode_type: str | None = None,
    decode_types: list[str] | None = None,
    klass: str | None = None,
    band: str | None = None,
    freq_min_hz: int | None = None,
    freq_max_hz: int | None = None,
    seen_after: str | None = None,
    seen_before: str | None = None,
    min_snr: float | None = None,
    decoded_only: bool = False,
    exclude_source: str | None = None,
    search: str | None = None,
    sort: str = "time",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    where.append(SPAN_INVARIANT)
    if section == "strongest":
        where.append("last_seen >= ?")
        params.append(local_day_start_utc(0))
    if exclude_source:
        # ADS-B aircraft have their own page. 1,203 aircraft episodes/day crowded
        # the signal list so badly that nothing else was readable.
        where.append("(source IS NULL OR source != ?)")
        params.append(exclude_source)
    if decode_type:
        where.append("decode_type = ?")
        params.append(decode_type)
    if decode_types:
        where.append("decode_type IN (" + ",".join("?" * len(decode_types)) + ")")
        params.extend(decode_types)
    if klass:
        where.append("class = ?")
        params.append(klass)
    if band:
        where.append("band LIKE ?")
        params.append(f"%{band}%")
    if freq_min_hz is not None:
        where.append("high_hz >= ?")
        params.append(int(freq_min_hz))
    if freq_max_hz is not None:
        where.append("low_hz <= ?")
        params.append(int(freq_max_hz))
    if seen_after:
        where.append("last_seen >= ?")
        params.append(seen_after)
    if seen_before:
        where.append("first_seen <= ?")
        params.append(seen_before)
    if min_snr is not None:
        where.append("peak_snr_db >= ?")
        params.append(float(min_snr))
    if decoded_only:
        where.append("decode_status = 'decoded'")
    if search:
        where.append("(COALESCE(likely,'') LIKE ? OR COALESCE(band,'') LIKE ? OR COALESCE(decoded_summary,'') LIKE ?)")
        params.extend([f"%{search}%"] * 3)

    order_col = SORTABLE.get(sort, "last_seen")
    order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
    if section == "strongest" and sort == "time":
        order_col, order_dir = "peak_snr_db", "DESC"

    # Decode honesty: "we tried every decoder and found no frame" and "never tried"
    # are different facts, and the row must be able to say which. Two correlated
    # subqueries (attempts are ~1k rows, indexed on episode_id) keep the page from
    # inferring one from the other.
    sql = (
        "SELECT e.*,"
        " (SELECT COUNT(*) FROM decode_attempts d WHERE d.episode_id = e.id) AS attempt_count,"
        " (SELECT GROUP_CONCAT(DISTINCT d.decoder) FROM decode_attempts d"
        "   WHERE d.episode_id = e.id AND d.status != 'ok') AS attempt_decoders"
        " FROM episodes e"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order_col} {order_dir} LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])

    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_episodes(**kwargs: Any) -> int:
    """Row count for the current filter set (bounded - the 1 GB cap keeps this small)."""
    kwargs = dict(kwargs)
    kwargs["limit"] = 20000
    kwargs["offset"] = 0
    return len(query_episodes(**kwargs))


def dashboard_counts(exclude_source: str | None = None) -> dict[str, Any]:
    """Counts for the Signals strip.

    exclude_source keeps the headline numbers consistent with the list beneath them -
    otherwise the strip counts ADS-B aircraft that the list deliberately hides.
    """
    today = local_day_start_utc(0)
    src = " AND (source IS NULL OR source != ?)" if exclude_source else ""
    span = " AND " + SPAN_INVARIANT
    params: tuple[Any, ...] = (today,) + ((exclude_source,) if exclude_source else ())
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE last_seen >= ?" + src + span, params).fetchone()[0]
        new = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE last_seen >= ? AND is_new=1" + src + span, params).fetchone()[0]
        decoded = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE last_seen >= ? AND decode_status='decoded'" + src + span,
            params).fetchone()[0]
        by_type = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT decode_type, COUNT(*) FROM episodes WHERE last_seen >= ? AND decode_type IS NOT NULL"
                + src + span + " GROUP BY decode_type",
                params,
            ).fetchall()
        }
        last_params: tuple[Any, ...] = (exclude_source,) if exclude_source else ()
        last = conn.execute(
            "SELECT MAX(last_seen) FROM episodes WHERE 1=1" + src + span, last_params).fetchone()[0]
    return {
        "today_total": total,
        "today_new": new,
        "today_decoded": decoded,
        "by_type": by_type,
        "last_seen": last,
    }


def get_episode(episode_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM episodes WHERE id=?", (int(episode_id),)).fetchone()
        if row is None:
            return None
        episode = dict(row)
        episode["artifacts"] = [
            dict(a)
            for a in conn.execute(
                "SELECT * FROM artifacts WHERE episode_id=? AND purged=0 ORDER BY kind", (int(episode_id),)
            ).fetchall()
        ]
        episode["labels"] = [
            dict(a)
            for a in conn.execute(
                "SELECT * FROM labels WHERE episode_id=? ORDER BY created_at DESC", (int(episode_id),)
            ).fetchall()
        ]
        episode["attempts"] = [
            dict(a)
            for a in conn.execute(
                "SELECT * FROM decode_attempts WHERE episode_id=? ORDER BY started_at DESC LIMIT 10",
                (int(episode_id),),
            ).fetchall()
        ]
    return episode


def add_label(episode_id: int, label: str, note: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO labels (episode_id, label, note, created_at) VALUES (?,?,?,?)",
            (int(episode_id), label, note, utcnow()),
        )
        if label in ("known", "ignore"):
            episode = conn.execute("SELECT freq_hz, bandwidth_hz FROM episodes WHERE id=?", (int(episode_id),)).fetchone()
            if episode is not None:
                half = max(int(episode["bandwidth_hz"]) // 2, 500_000)
                conn.execute(
                    "INSERT INTO known_emitters (freq_low_hz, freq_high_hz, name, note, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (
                        int(episode["freq_hz"]) - half,
                        int(episode["freq_hz"]) + half,
                        note or f"operator {label}",
                        f"from episode {episode_id}",
                        utcnow(),
                    ),
                )
            conn.execute("UPDATE episodes SET class='known', known_emitter=? WHERE id=?",
                         (note or f"operator {label}", int(episode_id)))
        conn.commit()


def pending_capture(limit: int = 3, min_score: float = 45.0) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes WHERE decode_status='none' AND capture_id IS NULL"
            " AND score >= ? ORDER BY score DESC, last_seen DESC LIMIT ?",
            (float(min_score), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]
