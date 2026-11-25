"""Index + search engine (v2).

Index   : SQLite file on disk. FTS5 table for words (name / tags / path / contents) plus a
          trigram table for fast substring + typo-tolerant file-name search.
Updates : strictly incremental. A file is re-read only if its size/mtime changed (or the
          extractor learned a new format). Unchanged files cost zero writes. With the
          `watchdog` package installed, changes are applied live (seconds); the periodic
          full scan is only a safety net.
Search  : multi-signal ranking (name match quality, BM25 over tags/contents, recency,
          how often you opened the file) with filters: ext: type: in: size: modified: "phrase" -exclude
"""
import difflib
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import extractors

HL_START, HL_END = "\x02", "\x03"      # markers FTS5 puts around matched words in snippets
SCHEMA_VERSION = 3
LIST_LIMIT = 200          # file-list searches (*.pdf) show more rows than word searches

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT UNIQUE NOT NULL,
    dir          TEXT NOT NULL,
    name         TEXT NOT NULL,
    ext          TEXT NOT NULL,
    size         INTEGER,
    mtime        REAL,
    content_done INTEGER DEFAULT 0,
    ex_ver       INTEGER DEFAULT 0,
    info         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, meta, path, content,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '2 3'
);
CREATE TABLE IF NOT EXISTS usage (
    path        TEXT PRIMARY KEY,
    opens       INTEGER DEFAULT 0,
    last_opened REAL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value
);
"""


def _migrate_v2_to_v3(conn):
    """v2 indexed 'report.pdf' as the words 'report' + 'pdf', so searching 'pdf' matched every PDF.
    v3 indexes only the file-name stem. Rewrites the name columns in place - files are not re-read."""
    conn.create_function("stemv", 1, lambda n: _name_variants(os.path.splitext(n)[0]))
    conn.create_function("stemo", 1, lambda n: os.path.splitext(n)[0])
    try:
        conn.execute("CREATE VIRTUAL TABLE fts_new USING fts5(name, meta, path, content, "
                     "tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
        conn.execute("INSERT INTO fts_new(rowid,name,meta,path,content) "
                     "SELECT f.id, stemv(f.name), x.meta, x.path, x.content FROM fts x JOIN files f ON f.id=x.rowid")
        conn.execute("DROP TABLE fts")
        conn.execute("ALTER TABLE fts_new RENAME TO fts")
        if has_trigram(conn):
            conn.execute("DELETE FROM fts_name")
            conn.execute("INSERT INTO fts_name(rowid,name) SELECT id, stemo(name) FROM files")
        conn.commit()
    except sqlite3.Error as exc:               # fall back to a clean rebuild
        print("migration failed, rebuilding index:", exc)
        conn.rollback()
        for t in ("fts_new", "fts", "fts_name", "files"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()


def connect(db_file):
    conn = sqlite3.connect(db_file, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA journal_size_limit=67108864")     # shrink the -wal file back to 64 MB after checkpoints
    ver =conn.execute("PRAGMA user_version").fetchone()[0]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if (cols and "dir" not in cols) or 0 < ver < 2:        # v1 layout: rebuild automatically
        for t in ("fts", "fts_name", "files"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
    elif ver == 2:
        _migrate_v2_to_v3(conn)
    conn.executescript(SCHEMA)
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_name USING fts5(name, tokenize='trigram')")
    except sqlite3.OperationalError:         # very old SQLite without trigram support
        pass
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn


def has_trigram(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='fts_name'").fetchone() is not None


@dataclass
class Result:
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    info: str = ""
    snippet: str = ""
    score: float = 0.0
    reason: str = ""          # why it matched: name / tags / contents / folder / similar name
    fid: int = 0              # files.id, used to build the content snippet later
    expr: str = ""            # FTS expression that matched this file ("" = matched by name only)
    detailed: bool = False    # snippet + reason filled (done only for rows that are on screen)
    fuzzy: bool = False       # found only by typo tolerance


# --------------------------------------------------------------------------- query parsing

_TOKEN_RE = re.compile(r'"([^"]+)"|(\S+)')
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)      # words, underscore is a separator like in FTS
_UNITS = {"": 1, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


@dataclass
class Parsed:
    terms: list
    phrases: list
    excludes: list
    exts: set = None
    dir_like: str = None
    min_size: int = None
    max_size: int = None
    after: float = None
    before: float = None
    name_terms: list = field(default_factory=list)      # name:foo  -> file names only
    content_terms: list = field(default_factory=list)   # content:foo -> inside files only
    globs: list = field(default_factory=list)           # SQL LIKE patterns applied to the file name
    glob_raw: list = field(default_factory=list)        # what the user typed, for display
    glob_terms: list = field(default_factory=list)      # literal words inside patterns (ranking/highlight)
    prefilter: list = field(default_factory=list)       # >=3 char literal runs for the trigram index
    match_all: bool = False                             # bare '*'
    hint: str = ""

    @property
    def search_words(self):
        """Words the user wants *found* (in names, tags or contents)."""
        out = list(self.terms) + list(self.name_terms) + list(self.content_terms)
        for ph in self.phrases:
            out += _WORD_RE.findall(ph.lower())
        return out

    @property
    def words(self):
        """Everything worth highlighting."""
        return self.search_words + list(self.glob_terms)

    @property
    def has_filter(self):
        return (self.match_all or bool(self.globs)
                or any(x is not None for x in (self.exts, self.dir_like, self.min_size, self.max_size,
                                               self.after, self.before)))

    @property
    def is_list(self):
        """File-list search such as *.pdf or report*.docx: no words to look for, only a file pattern."""
        return not self.search_words and self.has_filter

    def describe(self):
        """One-line explanation of how the query was understood."""
        parts = []
        if self.exts:
            parts.append(", ".join(sorted(self.exts)) + " files" if len(self.exts) <= 4 else "selected file types")
        if self.globs:
            parts.append("names like " + " ".join(self.glob_raw))
        if self.dir_like:
            parts.append(f"folder contains \u201c{self.dir_like}\u201d")
        if self.min_size is not None or self.max_size is not None:
            parts.append("size filter")
        if self.after is not None or self.before is not None:
            parts.append("date filter")
        if self.search_words:
            only_name = self.name_terms and not (self.terms or self.phrases or self.content_terms)
            only_content = self.content_terms and not (self.terms or self.phrases or self.name_terms)
            tail = ("file names only" if only_name else "inside files only" if only_content
                    else "names, tags and contents")
            label = " \u00b7 ".join(parts + [tail])
        elif parts:
            label = "Only " + " \u00b7 ".join(parts)
        else:
            label = "All files, newest first"
        return label + (f"   \u2014   {self.hint}" if self.hint else "")


def _parse_size(val):
    m = re.match(r"^([<>]=?)?(\d+(?:\.\d+)?)\s*(b|kb|mb|gb)?$", val.lower())
    if not m:
        return None, None
    op, num, unit = m.group(1) or ">", float(m.group(2)), m.group(3) or ""
    n = int(num * _UNITS[unit])
    return (n, None) if op.startswith(">") else (None, n)


def _parse_date(val):
    """-> (after_ts, before_ts); either may be None."""
    v = val.lower()
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rel = {"today": today, "yesterday": today - timedelta(days=1), "week": now - timedelta(days=7),
           "month": now - timedelta(days=30), "year": now - timedelta(days=365)}
    if v in rel:
        start = rel[v]
        if v == "yesterday":
            return start.timestamp(), today.timestamp()
        return start.timestamp(), None
    m = re.match(r"^([<>])?(\d{4}-\d{2}-\d{2})$", v)
    if m:
        try:
            d = datetime.strptime(m.group(2), "%Y-%m-%d")
        except ValueError:
            return None, None
        if m.group(1) == ">":
            return d.timestamp(), None
        if m.group(1) == "<":
            return None, d.timestamp()
        return d.timestamp(), (d + timedelta(days=1)).timestamp()
    return None, None


def _glob_to_like(g):
    out = []
    for ch in g:
        # a space also matches '_' or '-' ("annual report*" finds annual_report_2023.docx)
        out.append("%" if ch == "*" else "_" if ch in "? " else _like_escape(ch))
    return "".join(out)


def _is_glob(tok):
    # '?' is only a wildcard inside a token ("IMG_????.jpg"), not a trailing question mark ("really?")
    return "*" in tok or "?" in tok.rstrip("?")


def _add_ext(p, exts):
    p.exts = (p.exts or set()) | exts


def _add_glob(p, g, raw=None):
    """Wildcard pattern -> constraint on the file name. '*.pdf' is an extension test, not a word."""
    g = g.strip().lower()
    if not g:
        return
    m = re.fullmatch(r"\*\.\{([a-z0-9_,\-]+)\}", g)                     # *.{pdf,docx}
    if m:
        _add_ext(p, {"." + e for e in m.group(1).split(",") if e})
        return
    m = re.fullmatch(r"\*\.([a-z0-9_\-]+)", g)                          # *.pdf
    if m:
        _add_ext(p, {"." + m.group(1)})
        return
    if set(g) <= {"*"}:                                                 # *
        p.match_all = True
        return
    p.globs.append(_glob_to_like(g))
    p.glob_raw.append(raw or g)
    stem_part = re.sub(r"\.[a-z0-9]{1,6}$", "", g)                       # ignore a literal trailing .ext
    cleaned = re.sub(r"[*?]", " ", stem_part)
    p.glob_terms += _WORD_RE.findall(cleaned)
    p.prefilter += re.findall(r"[a-z0-9]{3,}", cleaned)


def parse_query(query):
    p = Parsed([], [], [])
    for m in _TOKEN_RE.finditer(query):
        phrase, tok = m.groups()
        if phrase:
            if _is_glob(phrase):                                        # "annual report*.docx"
                _add_glob(p, phrase, raw=f'"{phrase}"')
            elif len(phrase.split()) > 1:
                p.phrases.append(phrase.lower())
            else:
                p.terms += _WORD_RE.findall(phrase.lower())
            continue
        low = tok.lower()
        key, sep, val = low.partition(":")
        if sep and val and key in ("ext", "type", "in", "size", "modified", "date", "after", "before",
                                   "name", "content", "text"):
            if key in ("ext", "type"):
                for part in val.split(","):
                    cat = extractors.category_exts(part) if key == "type" else None
                    _add_ext(p, cat if cat else {"." + part.lstrip(".")})
            elif key == "name":
                p.name_terms += _WORD_RE.findall(val)
            elif key in ("content", "text"):
                p.content_terms += _WORD_RE.findall(val)
            elif key == "in":
                p.dir_like = val
            elif key == "size":
                lo, hi = _parse_size(val)
                p.min_size = lo if lo is not None else p.min_size
                p.max_size = hi if hi is not None else p.max_size
            elif key in ("modified", "date"):
                p.after, p.before = _parse_date(val)
            elif key == "after":
                p.after, _ = _parse_date(">" + val.lstrip(">"))
            elif key == "before":
                _, p.before = _parse_date("<" + val.lstrip("<"))
            continue
        if _is_glob(low):
            _add_glob(p, low)
            continue
        low = low.rstrip("?") or low
        m2 = re.fullmatch(r"\.([a-z0-9]{1,6})", low)                    # bare .pdf
        if m2 and "." + m2.group(1) in extractors.KNOWN_EXTS:
            _add_ext(p, {low})
            continue
        m2 = re.fullmatch(r"(.+)\.([a-z0-9]{1,6})", low)                # invoice.pdf -> a file name
        if m2 and "." + m2.group(2) in extractors.KNOWN_EXTS and _WORD_RE.search(m2.group(1)):
            _add_glob(p, f"*{m2.group(1)}*.{m2.group(2)}", raw=low)
            continue
        if low.startswith("-") and len(low) > 2:
            p.excludes += _WORD_RE.findall(low[1:])
            continue
        p.terms += _WORD_RE.findall(low)
    if (len(p.terms) == 1 and not (p.phrases or p.globs or p.exts or p.name_terms or p.content_terms)
            and "." + p.terms[0] in extractors.KNOWN_EXTS and len(p.terms[0]) >= 2):
        p.hint = f"Tip: *.{p.terms[0]} lists only .{p.terms[0]} files"
    return p


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _name_variants(name):
    """Index camelCase / letter-digit boundaries as extra words (IMG2024Report -> IMG 2024 Report)."""
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    split = re.sub(r"([A-Za-z])(\d)|(\d)([A-Za-z])", lambda m: (m.group(1) + " " + m.group(2))
                   if m.group(1) else (m.group(3) + " " + m.group(4)), split)
    return name if split == name else f"{name} {split}"


# --------------------------------------------------------------------------- search

def _locked(fn):
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


STEM_SQL = "substr(f.name, 1, length(f.name) - length(f.ext))"     # file name without its extension


class Searcher:
    """Thread-safe: one shared read connection, calls are serialised by a lock.

    How a query is understood (see parse_query):
      *.pdf  report*.docx  IMG_????.jpg  invoice.pdf   -> FILE patterns, matched against file names only
      plain words / "phrases"                           -> a file name OR tags OR words inside files
      name:foo / content:foo                            -> restrict to names / to file contents
    A pattern plus words (*.pdf tax) means: those kinds of files that mention the words.
    """

    def __init__(self, db_file):
        self.db_file = db_file
        self._lock = threading.RLock()
        self._shared = None
        self._stats_conn = None
        self._trigram = False
        self._is_stale = None       # set during search(): returns True once a newer query is waiting

    def _conn(self):
        if self._shared is None:
            self._shared = connect(self.db_file)
            self._trigram = has_trigram(self._shared)
            self._shared.set_progress_handler(self._progress, 20000)
        return self._shared

    def _progress(self):
        """Called by SQLite while a query runs; a non-zero return aborts it (the user typed on)."""
        stale = self._is_stale
        return 1 if stale is not None and stale() else 0

    # -- filters shared by all candidate queries (alias f = files)
    @staticmethod
    def _filters(p, extra_exts=None):
        sql, args = [], []
        exts = p.exts
        if extra_exts:
            exts = (exts & extra_exts) if exts else set(extra_exts)
        if exts is not None:
            if not exts:
                sql.append("0")
            else:
                sql.append(f"f.ext IN ({','.join('?' * len(exts))})")
                args += sorted(exts)
        for like in p.globs:
            sql.append("f.name LIKE ? ESCAPE '\\'")
            args.append(like)
        if p.dir_like:
            sql.append("f.dir LIKE ? ESCAPE '\\'")
            args.append(f"%{_like_escape(p.dir_like)}%")
        if p.min_size is not None:
            sql.append("f.size >= ?")
            args.append(p.min_size)
        if p.max_size is not None:
            sql.append("f.size <= ?")
            args.append(p.max_size)
        if p.after is not None:
            sql.append("f.mtime >= ?")
            args.append(p.after)
        if p.before is not None:
            sql.append("f.mtime < ?")
            args.append(p.before)
        return (" AND " + " AND ".join(sql)) if sql else "", args

    @_locked
    def recent(self, limit=12, category_exts=None):
        """Empty-query view: files you opened lately, else the newest files."""
        conn = self._conn()
        p = Parsed([], [], [])
        flt, args = self._filters(p, category_exts)
        rows = conn.execute(
            "SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info FROM usage u JOIN files f ON f.path=u.path "
            f"WHERE 1=1 {flt} ORDER BY u.last_opened DESC LIMIT ?", args + [limit]).fetchall()
        if len(rows) < limit:
            have = {r[0] for r in rows}
            more = conn.execute(
                f"SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info FROM files f WHERE 1=1 {flt} "
                "ORDER BY f.mtime DESC LIMIT ?", args + [limit * 2]).fetchall()
            rows += [r for r in more if r[0] not in have][:limit - len(rows)]
        return [Result(*r) for r in rows]

    @_locked
    def record_open(self, path):
        conn = self._conn()
        conn.execute("INSERT INTO usage(path,opens,last_opened) VALUES(?,1,?) "
                     "ON CONFLICT(path) DO UPDATE SET opens=opens+1, last_opened=excluded.last_opened",
                     (path, time.time()))
        conn.commit()

    def stats(self):
        """(files, files with contents). Slow on big indexes (~0.2 s): call it off the UI thread.
        Uses its own connection so it never holds up a search."""
        if self._stats_conn is None:
            self._stats_conn = sqlite3.connect(self.db_file, timeout=30, check_same_thread=False)
        conn = self._stats_conn
        n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        c = conn.execute("SELECT COUNT(*) FROM files WHERE content_done=1").fetchone()[0]
        return n, c

    @staticmethod
    def _add(cands, row, bm25=None, partial=False, fuzzy=0.0, fid=0, expr=""):
        path = row[0]
        c = cands.get(path)
        if c is None:
            c = cands[path] = dict(path=path, name=row[1], ext=row[2], size=row[3], mtime=row[4],
                                   info=row[5], bm25=None, partial=True, fuzzy=0.0, fid=0, expr="")
        if bm25 is not None and (c["bm25"] is None or bm25 < c["bm25"]):
            c["bm25"] = bm25
        if expr and not c["expr"]:
            c["fid"], c["expr"] = fid, expr
        c["partial"] = c["partial"] and partial
        c["fuzzy"] = max(c["fuzzy"], fuzzy)

    @_locked
    def fill_details(self, query, results):
        """Add the content snippet and the 'why it matched' tag to these results.
        Building a snippet reads the file's whole stored text, so it is done only for rows on screen."""
        todo = [r for r in results if not r.detailed]
        if not todo:
            return results
        conn = self._conn()
        for r in todo:
            if r.expr and r.fid:
                try:
                    row = conn.execute(
                        f"SELECT snippet(fts,3,'{HL_START}','{HL_END}',' … ',18) FROM fts "
                        "WHERE fts MATCH ? AND rowid=?", (r.expr, r.fid)).fetchone()
                except sqlite3.OperationalError:
                    row = None
                if row and row[0] and HL_START in row[0]:
                    r.snippet = row[0]
            r.detailed = True
        if query:
            p = parse_query(query)
            self._explain(conn, todo, p)
        return results

    @_locked
    def search(self, query, limit=50, category_exts=None, detail=5, is_stale=None):
        """Ranked results. Only the first `detail` get snippets/reasons; call fill_details for more.
        `is_stale()` returning True aborts the search early (returns []): a newer query replaced it."""
        self._is_stale = is_stale
        try:
            try:
                results = self._search(query, limit, category_exts)
            except sqlite3.OperationalError:
                if is_stale and is_stale():
                    return []
                raise
            if is_stale and is_stale():
                return []
            return self.fill_details(query, results[:detail]) + results[detail:]
        finally:
            self._is_stale = None

    def _search(self, query, limit, category_exts):
        p = parse_query(query)
        conn = self._conn()

        # ---- intent 1: a file pattern with no words (*.exe, report*.docx, ext:pdf) -> list files
        if not p.search_words:
            if p.has_filter or category_exts:
                return self._list_files(p, category_exts, max(limit, LIST_LIMIT))
            return []
        if not any(len(w) >= 2 for w in p.search_words):
            return []

        # ---- intent 2: words -> a file name, a tag, or text inside a file
        flt, fargs = self._filters(p, category_exts)
        cands = {}
        add = lambda *a, **k: self._add(cands, *a, **k)      # noqa: E731

        def fts_expr(joiner):
            # a 2-letter term ("mu" while typing "music") matches word starts in names/tags/folders only:
            # prefix-matching it against every file's text took ~0.3 s on a big index and could not be aborted
            parts = [f'"{t}"*' if len(t) >= 3 else f'{{name meta path}} : "{t}"*'
                     for t in p.terms if len(t) >= 2]
            parts += [f'"{ph}"' for ph in p.phrases]
            parts += [f'name : "{t}"*' for t in p.name_terms if len(t) >= 2]
            parts += [f'content : "{t}"*' for t in p.content_terms if len(t) >= 2]
            if not parts:
                return None
            expr = "(" + f" {joiner} ".join(parts) + ")"
            if p.excludes:
                expr += " NOT (" + " OR ".join(f'"{x}"*' for x in p.excludes) + ")"
            return expr

        def run_fts(expr, partial):
            # no snippet() here: it reads each file's full text and made big indexes ~50x slower
            sql = ("SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info,f.id, bm25(fts,10.0,6.0,1.5,1.0) "
                   f"FROM fts JOIN files f ON f.id=fts.rowid WHERE fts MATCH ? {flt} "
                   "ORDER BY 8 LIMIT 300")
            try:
                for row in conn.execute(sql, [expr] + fargs):
                    add(row[:6], bm25=row[7], partial=partial, fid=row[6], expr=expr)
            except sqlite3.OperationalError:
                pass

        expr = fts_expr("AND")
        n_parts = len(p.terms) + len(p.phrases) + len(p.name_terms) + len(p.content_terms)
        if expr:
            run_fts(expr, partial=False)
            if len(cands) < 5 and n_parts > 1 and not (p.name_terms or p.content_terms):
                run_fts(fts_expr("OR"), partial=True)             # relax: any word matches

        # substring match inside the file *name* (finds 'port' in 'report'); the extension is not searched
        name_words = [t for t in (p.terms + p.name_terms)]
        if name_words and not p.content_terms:
            tri_terms = [t for t in name_words if len(t) >= 3]
            if tri_terms and self._trigram:
                short_terms = [t for t in name_words if len(t) < 3]
                like_sql = "".join(f" AND {STEM_SQL} LIKE ? ESCAPE '\\'" for _ in short_terms)
                like_args = [f"%{_like_escape(t)}%" for t in short_terms]
                sql = ("SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info FROM fts_name "
                       f"JOIN files f ON f.id=fts_name.rowid WHERE fts_name MATCH ? {like_sql} {flt} "
                       "ORDER BY f.mtime DESC LIMIT 200")
                args = [" AND ".join(f'"{t}"' for t in tri_terms)] + like_args + fargs
            elif self._trigram:
                sql = None      # only 1-2 letter words: a LIKE scan of every name is slow; FTS above covers word starts
            else:
                like_sql = "".join(f" AND {STEM_SQL} LIKE ? ESCAPE '\\'" for _ in name_words)
                like_args = [f"%{_like_escape(t)}%" for t in name_words]
                sql = ("SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info FROM files f "
                       f"WHERE 1=1 {like_sql} {flt} ORDER BY f.mtime DESC LIMIT 200")
                args = like_args + fargs
            try:
                for row in (conn.execute(sql, args) if sql else ()):
                    if not any(x in row[1].lower() for x in p.excludes):
                        add(row)
                        cands[row[0]]["partial"] = False
            except sqlite3.OperationalError:
                pass

        if len(cands) < 5 and self._trigram and p.terms:           # typo tolerance when little was found
            self._fuzzy(conn, p, flt, fargs, add)

        return self._finish(conn, cands, p, limit)

    def _list_files(self, p, category_exts, limit):
        """File-list intent: *.exe, report*.docx, ext:pdf ... matched against names only."""
        conn = self._conn()
        flt, fargs = self._filters(p, category_exts)
        cols = "f.path,f.name,f.ext,f.size,f.mtime,f.info"
        if p.prefilter and self._trigram:      # narrow with the trigram index, LIKE then makes it exact
            sql = (f"SELECT {cols} FROM fts_name JOIN files f ON f.id=fts_name.rowid "
                   f"WHERE fts_name MATCH ? {flt} ORDER BY f.mtime DESC LIMIT 3000")
            args = [" AND ".join(f'"{t}"' for t in p.prefilter)] + fargs
        else:
            sql = f"SELECT {cols} FROM files f WHERE 1=1 {flt} ORDER BY f.mtime DESC LIMIT 3000"
            args = fargs
        cands = {}
        for row in conn.execute(sql, args):
            if not any(x in row[1].lower() for x in p.excludes):
                self._add(cands, row)
                cands[row[0]]["partial"] = False
        return self._finish(conn, cands, p, limit)

    def _finish(self, conn, cands, p, limit):
        """Score, sort, trim, and explain why each result matched."""
        now = time.time()
        usage = {}
        paths = list(cands)
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            for pth, opens, last in conn.execute(
                    f"SELECT path,opens,last_opened FROM usage WHERE path IN ({','.join('?' * len(chunk))})",
                    chunk):
                usage[pth] = (opens, last)
        results = []
        for c in cands.values():
            score = self._score(c, p, usage.get(c["path"]), now)
            results.append(Result(c["path"], c["name"], c["ext"], c["size"], c["mtime"],
                                  c["info"] or "", "", score, fid=c["fid"], expr=c["expr"],
                                  fuzzy=bool(c["fuzzy"])))
        results.sort(key=lambda r: -r.score)
        return results[:limit]

    @staticmethod
    def _explain(conn, results, p):
        terms = p.search_words
        if not terms or not results:
            return
        meta = {}
        paths = [r.path for r in results]
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            for pth, m in conn.execute(
                    "SELECT f.path, x.meta FROM fts x JOIN files f ON f.id=x.rowid "
                    f"WHERE f.path IN ({','.join('?' * len(chunk))})", chunk):
                meta[pth] = (m or "").lower()
        for r in results:
            stem = os.path.splitext(r.name)[0].lower()
            why = []
            if any(t in stem for t in terms if t not in p.content_terms):
                why.append("name")
            if any(t in meta.get(r.path, "") for t in terms if t not in p.content_terms):
                why.append("tags")
            if r.snippet:
                why.append("contents")
            if not why and any(t in os.path.dirname(r.path).lower() for t in terms):
                why.append("folder")
            if not why and r.fuzzy:
                why.append("similar name")
            r.reason = " \u00b7 ".join(why)

    def _fuzzy(self, conn, p, flt, fargs, add):
        for term in [t for t in p.terms if len(t) >= 4][:3]:
            tris = {term[i:i + 3] for i in range(len(term) - 2)}
            match = " OR ".join(f'"{t}"' for t in sorted(tris)[:40])
            sql = ("SELECT f.path,f.name,f.ext,f.size,f.mtime,f.info FROM fts_name "
                   f"JOIN files f ON f.id=fts_name.rowid WHERE fts_name MATCH ? {flt} "
                   "ORDER BY bm25(fts_name) LIMIT 400")
            try:
                rows = conn.execute(sql, [match] + fargs).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                tokens = _WORD_RE.findall(os.path.splitext(row[1])[0].lower())
                best = max((difflib.SequenceMatcher(None, term, tok).ratio() for tok in tokens), default=0)
                if best >= 0.75:
                    add(row, fuzzy=best)

    @staticmethod
    def _score(c, p, usage, now):
        name = c["name"].lower()
        stem = os.path.splitext(name)[0]          # the extension never counts as a name match
        toks = _WORD_RE.findall(stem)
        terms = p.terms + p.name_terms + p.glob_terms

        # name match quality 0..1
        if terms:
            if len(terms) == 1 and stem == terms[0]:
                q = 1.0
            elif stem.startswith(terms[0]) and all(t in stem for t in terms):
                q = 0.85
            elif all(any(tok.startswith(t) for tok in toks) for t in terms):
                q = 0.7                                   # every term starts a word in the name
            elif all(t in stem for t in terms):
                q = 0.5                                   # substrings only
            else:
                hit = sum(1 for t in terms if t in stem)
                q = 0.25 * hit / len(terms)
            if c["fuzzy"]:
                q = max(q, 0.55 * c["fuzzy"])
        else:
            q = 0.0

        ts = 0.0
        if c["bm25"] is not None:
            ts = 1.0 - math.exp(-max(0.0, -c["bm25"]) / 8.0)    # bm25 is negative; stronger = bigger

        age_days = max(0.0, (now - (c["mtime"] or 0)) / 86400.0)
        recency = math.exp(-age_days / 240.0)
        score = 2.4 * q + 1.3 * ts + 0.25 * recency
        if usage:
            opens, last = usage
            score += 0.35 * min(1.0, math.log1p(opens) / math.log(11))
            if last and now - last < 3 * 86400:
                score += 0.15
        if not c["partial"]:
            score += 0.3                                   # all words matched
        depth = c["path"].count(os.sep) + c["path"].count("/") * (os.sep != "/")
        score -= 0.01 * min(depth, 12)
        return score


# --------------------------------------------------------------------------- indexer

IGNORED_NAMES = (".tmp", ".crdownload", ".part", ".partial", ".swp")
TAG_EXTS = extractors.AUDIO_EXTS | extractors.VIDEO_TAG_EXTS      # music/video tags: always read


def content_exts(cfg):
    """File types whose text is indexed: the 'content_exts' setting plus music/video tags."""
    chosen = cfg.get("content_exts")
    if chosen is None:
        return extractors.CONTENT_EXTS
    return ({("." + e.lower().lstrip(".")) for e in chosen} | TAG_EXTS) & extractors.CONTENT_EXTS


def prune_content(conn, allowed, log=print):
    """Drop stored text of every file whose type is not in `allowed`, then compact the database.
    Rebuilds the word table in one pass (much faster than deleting rows one by one from FTS5).
    Returns (files stripped, bytes before, bytes after)."""
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    before = conn.execute("PRAGMA page_count").fetchone()[0] * page
    allowed = sorted(allowed)
    ph = ",".join("?" * len(allowed))
    n = conn.execute(f"SELECT COUNT(*) FROM files WHERE content_done=1 AND ext NOT IN ({ph})",
                     allowed).fetchone()[0]
    if n:
        log(f"stripping text from {n:,} files ...")
        conn.execute("DROP TABLE IF EXISTS fts_new")
        conn.execute("CREATE VIRTUAL TABLE fts_new USING fts5(name, meta, path, content, "
                     "tokenize='unicode61 remove_diacritics 2', prefix='2 3')")
        conn.execute(
            "INSERT INTO fts_new(rowid,name,meta,path,content) "
            f"SELECT x.rowid, x.name, CASE WHEN f.ext IN ({ph}) THEN x.meta ELSE '' END, x.path, "
            f"CASE WHEN f.ext IN ({ph}) THEN x.content ELSE '' END "
            "FROM fts x JOIN files f ON f.id=x.rowid", allowed + allowed)
        conn.execute("DROP TABLE fts")
        conn.execute("ALTER TABLE fts_new RENAME TO fts")
        conn.execute(f"UPDATE files SET content_done=0, ex_ver=0, info='' "
                     f"WHERE content_done=1 AND ext NOT IN ({ph})", allowed)
        conn.commit()
    log("compacting database ...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = conn.execute("PRAGMA page_count").fetchone()[0] * page
    return n, before, after


class Indexer:
    """Background thread: initial/safety scans + live updates from the file system watcher."""

    def __init__(self, db_file, get_config):
        self.db_file = db_file
        self.get_config = get_config
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._force = False
        self._rebuild = False
        self._last_yield = time.monotonic()
        self._ev_lock = threading.Lock()
        self._events = set()
        self._observer = None
        self._watched = set()
        self.watching = False
        self.state = "idle"                   # idle | scanning | updating
        self.seen = 0
        self.updated = 0
        self.last_finished = None
        self.last_event_at = None
        self.index_ready = False              # a full scan has finished once (persisted in the db)
        self._tri = False

    # ---- public API
    def start(self):
        self._thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._observer:
            try:
                self._observer.stop()
            except Exception:
                pass

    def rescan_now(self):
        self._force = True
        self._wake.set()

    def rebuild(self):
        self._rebuild = True
        self._force = True
        self._wake.set()

    def status_text(self):
        if self.state == "scanning":
            if not self.index_ready:          # first build of the index
                return f"Indexing… {self.seen:,} checked, {self.updated:,} new/changed"
            # routine safety re-check of an index that is already built and searchable
            changed = f", {self.updated:,} updated" if self.updated else ""
            return f"Up to date · checking for changes ({self.seen:,} checked{changed})"
        if self.last_finished:
            return "Up to date · watching for changes" if self.watching else "Up to date"
        return "Waiting to index"

    # ---- file system watcher (optional dependency)
    def _push_event(self, path):
        with self._ev_lock:
            self._events.add(path)
        self._wake.set()

    def _sync_watches(self, cfg):
        roots = {r for r in cfg["folders"] if os.path.isdir(r)}
        if roots == self._watched and (self._observer or not roots):
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            self.watching = False
            return
        if self._observer:
            try:
                self._observer.stop()
            except Exception:
                pass
            self._observer = None
        self._watched = roots
        if not roots:
            self.watching = False
            return
        push = self._push_event

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed", "closed_no_write"):
                    return
                if event.is_directory and event.event_type == "modified":
                    return
                push(event.src_path)
                dest = getattr(event, "dest_path", "")
                if dest:
                    push(dest)

        obs = Observer()
        ok = 0
        for r in roots:
            try:
                obs.schedule(Handler(), r, recursive=True)
                ok += 1
            except Exception as exc:
                print("cannot watch", r, exc)
        if ok:
            obs.daemon = True
            obs.start()
            self._observer = obs
        self.watching = ok > 0

    # ---- persisted scan state
    def _load_state(self, conn):
        row = conn.execute("SELECT value FROM state WHERE key='last_full_scan'").fetchone()
        pending = conn.execute("SELECT 1 FROM state WHERE key='first_scan_pending'").fetchone()
        if row and row[0]:
            self.last_finished = float(row[0])
            self.index_ready = True
        elif not pending and conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
            self.index_ready = True           # index built by an older DeskSearch (no state row yet)

    def _startup_scan_delay(self, conn, interval):
        """Seconds until the first full re-check after start. A restart soon after a finished scan
        does not walk every drive again; the watcher covers changes in the meantime."""
        if not self.last_finished:
            return 0
        age = time.time() - self.last_finished
        return max(0.0, interval - age) if 0 <= age < interval else 0

    # ---- main loop
    def _run(self):
        conn = connect(self.db_file)
        self._tri = has_trigram(conn)      # looked up once, not per file
        self._load_state(conn)
        cfg = self.get_config()
        try:
            self._sync_watches(cfg)
        except Exception as exc:
            print("watcher error:", exc)
        interval = max(1, cfg["rescan_minutes"]) * 60
        if not self.watching:
            interval = min(interval, 15 * 60)
        # scan immediately on start, unless a full scan finished recently and the watcher is live
        delay = self._startup_scan_delay(conn, interval) if self.watching else 0
        last_scan = time.monotonic() - interval + delay if delay else -1e12
        while not self._stop.is_set():
            cfg = self.get_config()
            self._wake.clear()
            if self._rebuild:
                self._rebuild = False
                conn.execute("DELETE FROM fts")
                conn.execute("DELETE FROM fts_name") if self._tri else None
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM state WHERE key='last_full_scan'")
                conn.commit()
                self.index_ready = False
                self.last_finished = None
            try:
                self._sync_watches(cfg)
            except Exception as exc:
                print("watcher error:", exc)
            interval = max(1, cfg["rescan_minutes"]) * 60
            if not self.watching:
                interval = min(interval, 15 * 60)
            due = self._force or (time.monotonic() - last_scan >= interval)
            if cfg["folders"] and due:
                self._force = False
                try:
                    self._scan(conn, cfg)
                except Exception as exc:
                    print("indexer error:", exc)
                    self.state = "idle"
                last_scan = time.monotonic()
            if self._events:
                self._stop.wait(1.0)              # debounce bursts (copying many files, saving)
                with self._ev_lock:
                    paths, self._events = self._events, set()
                try:
                    self._apply_events(conn, cfg, paths)
                except Exception as exc:
                    print("event error:", exc)
                    self.state = "idle"
            remaining = max(1.0, interval - (time.monotonic() - last_scan))
            self._wake.wait(timeout=min(remaining, 60))

    # ---- helpers
    @staticmethod
    def _excluded_path(path, cfg):
        """True if the path lies inside a skipped folder *below* its indexed root."""
        ex = {d.lower() for d in cfg["excluded_dirs"]}
        norm = path.replace("\\", "/")
        rel = norm
        for r in cfg["folders"]:
            rn = r.replace("\\", "/").rstrip("/")
            if norm.lower().startswith(rn.lower() + "/"):
                rel = norm[len(rn) + 1:]
                break
        parts = rel.lower().split("/")
        if cfg.get("skip_hidden_folders", True) and any(x.startswith(".") and x not in ("", ".", "..")
                                                        for x in parts[:-1]):
            return True
        return any(x in ex for x in parts[:-1]) or parts[-1] in ex

    def _delete_ids(self, conn, ids):
        has_tri = self._tri
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM fts WHERE rowid IN ({ph})", chunk)
            if has_tri:
                conn.execute(f"DELETE FROM fts_name WHERE rowid IN ({ph})", chunk)
            conn.execute(f"DELETE FROM files WHERE id IN ({ph})", chunk)

    def _index_file(self, conn, cfg, path, name, directory, st, row):
        """Insert/update one file. `row` = existing (id,size,mtime,content_done,ex_ver) or None.
        Returns True if the index was written."""
        ext = os.path.splitext(name)[1].lower()
        max_bytes = cfg["max_content_file_mb"] * 1024 * 1024
        wanted = cfg["index_contents"] and ext in content_exts(cfg)
        can_content = (wanted
                       and (st.st_size <= max_bytes or ext in extractors.AUDIO_EXTS)
                       and extractors.available(ext))      # missing library -> retried after install
        if row:
            same = row[1] == st.st_size and abs(row[2] - st.st_mtime) < 2
            upgrade = can_content and (not row[3] or row[4] < extractors.EXTRACTOR_VERSION)
            drop = bool(row[3]) and not wanted             # type removed from the list: forget its text
            if same and not upgrade and not drop:
                return False
        meta = content = info = ""
        if can_content:
            meta, content, info = extractors.extract(path, ext, cfg["max_content_chars"])
            now = time.monotonic()                # duty-cycle throttle: ~0.1s rest per 0.5s of work
            if now - self._last_yield > 0.5:
                time.sleep(0.1)
                self._last_yield = time.monotonic()
        fid = row[0] if row else None
        if fid:
            conn.execute("DELETE FROM fts WHERE rowid=?", (fid,))
            if self._tri:
                conn.execute("DELETE FROM fts_name WHERE rowid=?", (fid,))
            conn.execute("UPDATE files SET dir=?,name=?,ext=?,size=?,mtime=?,content_done=?,ex_ver=?,info=? "
                         "WHERE id=?", (directory, name, ext, st.st_size, st.st_mtime, int(can_content),
                                        extractors.EXTRACTOR_VERSION if can_content else 0, info, fid))
        else:
            cur = conn.execute(
                "INSERT INTO files(path,dir,name,ext,size,mtime,content_done,ex_ver,info) VALUES(?,?,?,?,?,?,?,?,?)",
                (path, directory, name, ext, st.st_size, st.st_mtime, int(can_content),
                 extractors.EXTRACTOR_VERSION if can_content else 0, info))
            fid = cur.lastrowid
        stem = os.path.splitext(name)[0]                  # the extension is stored, but is not a searchable word
        conn.execute("INSERT INTO fts(rowid,name,meta,path,content) VALUES(?,?,?,?,?)",
                     (fid, _name_variants(stem), meta, directory, content))
        if self._tri:
            conn.execute("INSERT INTO fts_name(rowid,name) VALUES(?,?)", (fid, stem))
        return True

    def _scan_tree(self, conn, cfg, start, visited):
        """Walk `start`, diffing each directory against the index one directory at a time."""
        excluded = {d.lower() for d in cfg["excluded_dirs"]}
        skip_dot = cfg.get("skip_hidden_folders", True)
        stack = [start]
        pending = 0
        while stack:
            if self._stop.is_set():
                return False
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            visited.add(d)
            known = {r[0]: r[1:] for r in conn.execute(
                "SELECT name,id,size,mtime,content_done,ex_ver FROM files WHERE dir=?", (d,))}
            present = set()
            with it:
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if name.lower() not in excluded and not (skip_dot and name.startswith(".")):
                                stack.append(entry.path)
                            continue
                        if not entry.is_file():
                            continue
                        st = entry.stat()
                    except OSError:
                        continue
                    present.add(name)
                    self.seen += 1
                    if name.endswith(IGNORED_NAMES) or name.startswith("~$"):
                        continue
                    try:
                        if self._index_file(conn, cfg, entry.path, name, d, st, known.get(name)):
                            self.updated += 1
                            pending += 1
                    except sqlite3.Error as exc:
                        print("db error:", exc)
            gone = [v[0] for k, v in known.items() if k not in present]
            if gone:
                self._delete_ids(conn, gone)
                pending += len(gone)
            if pending >= 200:
                conn.commit()
                pending = 0
        conn.commit()
        return True

    def _scan(self, conn, cfg):
        self.state, self.seen, self.updated = "scanning", 0, 0
        if not self.index_ready:              # remembered so an interrupted first build still counts as one
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('first_scan_pending',1)")
            conn.commit()
        visited = set()
        offline = []
        complete = True
        for root in cfg["folders"]:
            if not os.path.isdir(root):
                offline.append(root)               # unplugged drive: keep its entries
                continue
            if not self._scan_tree(conn, cfg, root, visited):
                complete = False
                break
        if complete:                               # forget directories that no longer exist / are excluded
            off = [r.rstrip("\\/") for r in offline]
            stale = []
            for (d,) in conn.execute("SELECT DISTINCT dir FROM files"):
                if d in visited:
                    continue
                if any(d == o or d.startswith(o + os.sep) or d.startswith(o + "/") for o in off):
                    continue
                stale.append(d)
            for d in stale:
                ids = [r[0] for r in conn.execute("SELECT id FROM files WHERE dir=?", (d,))]
                self._delete_ids(conn, ids)
            now = time.time()
            conn.execute("INSERT OR REPLACE INTO state(key,value) VALUES('last_full_scan',?)", (now,))
            conn.execute("DELETE FROM state WHERE key='first_scan_pending'")
            conn.commit()
            self.index_ready = True
        self.state = "idle"
        self.last_finished = time.time()

    def _apply_events(self, conn, cfg, paths):
        """Apply watcher events: only the touched paths are looked at."""
        self.state = "updating"
        for path in sorted(paths):
            if self._stop.is_set():
                break
            if self._excluded_path(path, cfg):
                continue
            name = os.path.basename(path)
            if name.endswith(IGNORED_NAMES) or name.startswith("~$"):
                continue
            try:
                if os.path.isdir(path):
                    self._scan_tree(conn, cfg, path, set())
                elif os.path.isfile(path):
                    st = os.stat(path)
                    row = conn.execute(
                        "SELECT id,size,mtime,content_done,ex_ver FROM files WHERE path=?", (path,)).fetchone()
                    self._index_file(conn, cfg, path, name, os.path.dirname(path), st, row)
                else:                               # deleted or moved away: drop it (and anything under it)
                    ids = [r[0] for r in conn.execute(
                        "SELECT id FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
                        (path, _like_escape(path + os.sep) + "%"))]
                    if ids:
                        self._delete_ids(conn, ids)
            except (OSError, sqlite3.Error) as exc:
                print("event skipped:", path, exc)
        conn.commit()
        self.state = "idle"
        self.last_event_at = time.time()
