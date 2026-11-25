"""Text/metadata extraction for the index.

extract() returns (meta, content, info):
  meta    - short high-value text (title, artist, album, author, keywords...) ranked above body text
  content - body text (document text, lyrics, ...)
  info    - one-line human summary shown in the result list (e.g. "Artist - Album | 3:45 | FLAC")
Optional libraries (mutagen, olefile, xlrd, pypdf) are imported lazily; a missing one
just means that file type is found by name only.
"""
import html
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

EXTRACTOR_VERSION = 2     # bump when new formats are added -> only affected files get re-read

# ----------------------------------------------------------------------- file families
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml",
    ".yml", ".ini", ".cfg", ".conf", ".toml", ".rst", ".tex", ".srt", ".vtt", ".sql",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".php", ".java", ".c",
    ".h", ".cpp", ".cs", ".go", ".rb", ".rs", ".sh", ".bat", ".ps1", ".vue",
    ".svelte", ".env", ".gitignore", ".svg", ".lrc", ".nfo", ".m3u", ".m3u8", ".pls",
}
HTML_EXTS = {".html", ".htm", ".xhtml", ".mht"}
WORD_X = {".docx", ".docm", ".dotx", ".dotm"}
SHEET_X = {".xlsx", ".xlsm", ".xltx", ".xltm"}
SLIDE_X = {".pptx", ".pptm", ".potx", ".potm", ".ppsx"}
ODF = {".odt", ".ods", ".odp", ".odg"}
OFFICE_X = WORD_X | SHEET_X | SLIDE_X | ODF
OLE_EXTS = {".doc", ".dot", ".xls", ".xlt", ".ppt", ".pps", ".pot"}
PDF_EXTS = {".pdf"}
RTF_EXTS = {".rtf"}
EPUB_EXTS = {".epub"}
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".wav",
              ".aiff", ".aif", ".ape", ".wv", ".mpc", ".m4b"}
VIDEO_TAG_EXTS = {".mp4", ".m4v", ".wmv", ".asf"}      # tags readable via mutagen

CONTENT_EXTS = (TEXT_EXTS | HTML_EXTS | OFFICE_X | OLE_EXTS | PDF_EXTS | RTF_EXTS
                | EPUB_EXTS | AUDIO_EXTS | VIDEO_TAG_EXTS)

CATEGORIES = {
    "documents": {".doc", ".docx", ".docm", ".dot", ".dotx", ".odt", ".rtf", ".pdf", ".txt",
                  ".md", ".epub", ".xls", ".xlsx", ".xlsm", ".ods", ".csv", ".ppt", ".pptx",
                  ".pps", ".ppsx", ".odp", ".tex", ".html", ".htm"},
    "music": AUDIO_EXTS,
    "pictures": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
                 ".svg", ".raw", ".cr2", ".nef", ".arw", ".dng", ".ico", ".psd"},
    "videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
               ".mpeg", ".3gp", ".ts"},
    "code": {".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".php", ".java", ".c", ".h",
             ".cpp", ".cs", ".go", ".rb", ".rs", ".sh", ".bat", ".ps1", ".vue", ".html", ".json",
             ".sql", ".xml", ".yml", ".yaml"},
    "sheets": {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".ods", ".csv", ".tsv"},
    "slides": {".ppt", ".pptx", ".pps", ".ppsx", ".pot", ".potx", ".odp"},
    "pdf": {".pdf"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "apps": {".exe", ".msi", ".lnk", ".appx", ".apk", ".scr"},
}
# every extension we recognise: lets 'invoice.pdf' be understood as a file name, not two words
KNOWN_EXTS = set().union(*CATEGORIES.values()) | TEXT_EXTS | OLE_EXTS | {
    ".dll", ".sys", ".bak", ".dat", ".db", ".sqlite", ".ai", ".eps", ".cab", ".reg", ".torrent",
    ".vcf", ".ics", ".eml", ".msg", ".mid", ".midi", ".ttf", ".otf", ".bin", ".img", ".dmg",
    ".jar", ".war", ".pyc", ".class", ".lock", ".map", ".psd", ".indd", ".xd", ".fig", ".sketch",
}
CATEGORY_ALIASES = {
    "doc": "documents", "docs": "documents", "document": "documents", "documents": "documents",
    "music": "music", "audio": "music", "song": "music", "songs": "music", "mp3": None,
    "pic": "pictures", "pics": "pictures", "image": "pictures", "images": "pictures",
    "photo": "pictures", "photos": "pictures", "picture": "pictures", "pictures": "pictures",
    "video": "videos", "videos": "videos", "movie": "videos", "movies": "videos",
    "app": "apps", "apps": "apps", "program": "apps", "programs": "apps", "software": "apps",
    "code": "code", "source": "code", "sheet": "sheets", "sheets": "sheets",
    "spreadsheet": "sheets", "slide": "slides", "slides": "slides", "ppt": None,
    "presentation": "slides", "pdf": "pdf", "archive": "archives", "archives": "archives",
}


def category_exts(name):
    """Resolve a user word ('music', 'docs'...) to a set of extensions, or None."""
    key = CATEGORY_ALIASES.get(name.lower())
    return CATEGORIES.get(key) if key else None


def category_of(ext):
    for cat in ("apps", "music", "pictures", "videos", "pdf", "sheets", "slides", "documents", "code",
                "archives"):
        if ext in CATEGORIES[cat]:
            return cat
    return "other"


# ----------------------------------------------------------------------- helpers
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


def _clean(text, limit):
    return _WS_RE.sub(" ", text).strip()[:limit]


def _read_text(path, limit):
    with open(path, "rb") as fh:
        raw = fh.read(limit * 4)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="ignore")
    if b"\x00" in raw[:2048]:
        return ""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="ignore")


def _strip_html(text):
    text = _SCRIPT_RE.sub(" ", text)
    return html.unescape(_TAG_RE.sub(" ", text))


def _xml_text(data, only_tag=None):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    if only_tag:
        parts = ("".join(e.itertext()) for e in root.iter()
                 if e.tag.rsplit("}", 1)[-1] == only_tag)
        return " ".join(p.strip() for p in parts if p.strip())
    return " ".join(t.strip() for t in root.itertext() if t and t.strip())


def _fmt_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------------------------------------------------- OOXML / ODF / EPUB
def _core_props(zf):
    """Title/author/subject/keywords from docProps/core.xml (OOXML) or meta.xml (ODF)."""
    for name in ("docProps/core.xml", "meta.xml"):
        if name in zf.namelist():
            return _xml_text(zf.read(name))
    return ""


def _office_x(path, ext, limit):
    out, size = [], 0
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        meta = _core_props(zf)
        if ext in WORD_X:
            parts = [n for n in names if n.startswith("word/") and n.endswith(".xml")
                     and not n.startswith(("word/theme", "word/_rels"))]
        elif ext in SHEET_X:
            parts = [n for n in names if n == "xl/sharedStrings.xml"] + \
                    sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        elif ext in SLIDE_X:
            parts = sorted(n for n in names if n.startswith(("ppt/slides/slide", "ppt/notesSlides/"))
                           and n.endswith(".xml"))
        else:
            parts = [n for n in names if n == "content.xml"]
        for name in parts:
            txt = (_xml_text(zf.read(name), only_tag="t") if name.startswith("xl/worksheets/")
                   else _xml_text(zf.read(name)))
            out.append(txt)
            size += len(txt)
            if size >= limit:
                break
    return meta, " ".join(out)


def _epub_text(path, limit):
    out, size = [], 0
    with zipfile.ZipFile(path) as zf:
        meta = ""
        for n in zf.namelist():
            if n.endswith(".opf"):
                meta = _xml_text(zf.read(n))
                break
        for n in zf.namelist():
            if n.lower().endswith((".xhtml", ".html", ".htm")):
                txt = _strip_html(zf.read(n).decode("utf-8", errors="ignore"))
                out.append(txt)
                size += len(txt)
                if size >= limit:
                    break
    return meta, " ".join(out)


# ----------------------------------------------------------------------- RTF
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)")
_RTF_SKIP_DEST = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer",
                  "headerl", "headerr", "footerl", "footerr", "themedata", "colorschememapping",
                  "latentstyles", "datastore", "listtable", "listoverridetable", "rsidtbl",
                  "generator", "xmlnstbl", "fldinst", "bkmkstart", "bkmkend", "object"}


def _rtf_text(path, limit):
    with open(path, "rb") as fh:
        data = fh.read(limit * 8).decode("latin-1")
    out, depth, skip_depth, first = [], 0, None, False
    for m in _RTF_TOKEN.finditer(data):
        word, num, hexv, sym, brace, text = m.groups()
        if brace == "{":
            depth += 1
            first = True
            continue
        if brace == "}":
            depth -= 1
            if skip_depth is not None and depth < skip_depth:
                skip_depth = None
            first = False
            continue
        if skip_depth is not None:
            continue
        if sym == "*" and first:
            skip_depth = depth
            continue
        first_here, first = first, False
        if word:
            if first_here and word in _RTF_SKIP_DEST:
                skip_depth = depth
            elif word in ("par", "line", "row"):
                out.append("\n")
            elif word == "tab":
                out.append(" ")
            elif word == "u" and num:
                out.append(chr(int(num) % 65536))
        elif hexv:
            out.append(bytes([int(hexv, 16)]).decode("cp1252", "ignore"))
        elif sym in ("~",):
            out.append(" ")
        elif text:
            out.append(text.replace("\r", "").replace("\n", ""))
    return "", "".join(out)


# ----------------------------------------------------------------------- legacy OLE: doc / xls / ppt
def _ole_meta(ole):
    try:
        m = ole.get_metadata()
        vals = [getattr(m, k, None) for k in ("title", "subject", "author", "keywords", "comments")]
        return " ".join(v.decode("cp1252", "ignore") if isinstance(v, bytes) else str(v)
                        for v in vals if v)
    except Exception:
        return ""


def _doc_text(ole):
    """Word 97-2003: parse FIB + piece table (CLX) for the main text."""
    wd = ole.openstream("WordDocument").read()
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    table = ole.openstream("1Table" if flags & 0x0200 else "0Table").read()
    pos = 32
    csw = struct.unpack_from("<H", wd, pos)[0]
    pos += 2 + csw * 2
    cslw = struct.unpack_from("<H", wd, pos)[0]
    lw_start = pos + 2
    ccp_text = struct.unpack_from("<i", wd, lw_start + 3 * 4)[0]
    pos = lw_start + cslw * 4
    pos += 2                                   # cbRgFcLcb
    fc_clx, lcb_clx = struct.unpack_from("<II", wd, pos + 33 * 8)
    clx = table[fc_clx:fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 1:        # skip Prc entries
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 2:
        raise ValueError("no piece table")
    plc_len = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + plc_len]
    n = (plc_len - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    out = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, (n + 1) * 4 + k * 8 + 2)[0]
        count = min(cps[k + 1], ccp_text) - cps[k]
        if count <= 0:
            continue
        if fc & 0x40000000:
            off = (fc & 0x3FFFFFFF) // 2
            out.append(wd[off:off + count].decode("cp1252", "ignore"))
        else:
            out.append(wd[fc:fc + count * 2].decode("utf-16-le", "ignore"))
    text = "".join(out)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x13\x14\x15]", " ", text).replace("\r", "\n")


def _doc_text_fallback(ole):
    """Last resort: pull printable runs out of the WordDocument stream."""
    wd = ole.openstream("WordDocument").read()
    runs = re.findall(rb"(?:[\x20-\x7e]\x00){5,}", wd)
    runs = [r.decode("utf-16-le", "ignore") for r in runs]
    runs += [r.decode("cp1252", "ignore") for r in re.findall(rb"[\x20-\x7e]{8,}", wd)]
    return " ".join(runs)


def _ppt_text(ole):
    """PowerPoint 97-2003: walk records, collect TextChars/TextBytes atoms."""
    data = ole.openstream("PowerPoint Document").read()
    out, pos, n = [], 0, len(data)
    while pos + 8 <= n:
        ver_inst, rtype, rlen = struct.unpack_from("<HHI", data, pos)
        pos += 8
        if ver_inst & 0xF == 0xF:              # container: descend into children
            continue
        body = data[pos:pos + rlen]
        piece = None
        if rtype == 0x0FA0:
            piece = body.decode("utf-16-le", "ignore")
        elif rtype == 0x0FA8:
            piece = body.decode("cp1252", "ignore")
        if piece:
            piece = piece.replace("\r", "\n").replace("\x0b", " ").strip()
            # drop master-slide boilerplate and 1-2 char placeholders
            if len(piece) > 2 and not piece.startswith(("Click to edit", "Second ", "Third ",
                                                        "Fourth ", "Fifth ")):
                out.append(piece)
        pos += rlen
    return " ".join(out)


def _xls_text(path, limit):
    import xlrd
    book = xlrd.open_workbook(path, on_demand=True, formatting_info=False)
    out, size = [], 0
    for sheet in book.sheets():
        out.append(sheet.name)
        for r in range(sheet.nrows):
            for cell in sheet.row(r):
                if cell.ctype == 1 and cell.value:
                    out.append(cell.value)
                    size += len(cell.value)
            if size >= limit:
                break
        book.unload_sheet(sheet.name)
        if size >= limit:
            break
    book.release_resources()
    return " ".join(out)


def _ole_extract(path, ext, limit):
    import olefile
    ole = olefile.OleFileIO(path)
    try:
        meta = _ole_meta(ole)
        if ext in (".doc", ".dot"):
            try:
                body = _doc_text(ole)
            except Exception:
                body = _doc_text_fallback(ole)
        elif ext in (".ppt", ".pps", ".pot"):
            body = _ppt_text(ole)
        else:
            body = ""
    finally:
        ole.close()
    if ext in (".xls", ".xlt"):
        body = _xls_text(path, limit)
    return meta, body


# ----------------------------------------------------------------------- PDF
def _pdf_extract(path, limit, max_pages=60):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return "", ""
    meta = ""
    try:
        md = reader.metadata or {}
        meta = " ".join(str(md.get(k)) for k in ("/Title", "/Author", "/Subject", "/Keywords") if md.get(k))
    except Exception:
        pass
    out, size = [], 0
    for i, page in enumerate(reader.pages):
        if i >= max_pages:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:
            continue
        out.append(txt)
        size += len(txt)
        if size >= limit:
            break
    return meta, " ".join(out)


# ----------------------------------------------------------------------- audio / video tags
def _first(tags, *keys):
    low = {str(k).lower(): v for k, v in tags.items()}
    for k in keys:
        v = low.get(k.lower())
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        v = str(v).strip()
        if v:
            return v
    return ""


def _audio_extract(path, ext):
    import mutagen
    f = mutagen.File(path, easy=True)
    if f is None:
        return "", "", ""
    tags = dict(f.tags.items()) if f.tags else {}
    title = _first(tags, "title", "Title")
    artist = _first(tags, "artist", "Author", "WM/Author")
    album = _first(tags, "album", "WM/AlbumTitle")
    albumartist = _first(tags, "albumartist", "WM/AlbumArtist")
    genre = _first(tags, "genre", "WM/Genre")
    year = _first(tags, "date", "year", "WM/Year")[:4]
    composer = _first(tags, "composer", "WM/Composer")

    # every other short text tag (comment, grouping, label...) helps findability
    extra, seen = [], {title, artist, albumartist, album, genre, year, composer}
    for k, v in tags.items():
        if str(k).lower() in ("lyrics", "unsyncedlyrics", "lyrics-eng"):
            continue
        vals = v if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if isinstance(x, (str, int)) or hasattr(x, "value"):
                sx = str(x).strip()
                if sx and sx not in seen and len(sx) < 300:
                    seen.add(sx)
                    extra.append(sx)
    meta = " ".join(x for x in (title, artist, albumartist, album, genre, year, composer) if x)
    meta = _clean(meta + " " + " ".join(extra), 2000)

    lyrics = ""
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            t = ID3(path)
            lyrics = " ".join(" ".join(fr.text.split()) if hasattr(fr, "text") and isinstance(fr.text, str)
                              else str(fr) for fr in t.getall("USLT"))
            for fr in t.getall("COMM"):
                meta += " " + " ".join(map(str, fr.text))
        else:
            lyrics = _first(tags, "lyrics", "unsyncedlyrics")
    except Exception:
        pass

    parts = []
    line = " \u2014 ".join(x for x in (artist or albumartist, album) if x)
    if title:
        line = f"{title}  \u00b7  {line}" if line else title
    if line:
        parts.append(line)
    if year:
        parts.append(year)
    length = getattr(f.info, "length", 0)
    if length:
        parts.append(_fmt_duration(length))
    br = getattr(f.info, "bitrate", 0)
    if br:
        parts.append(f"{int(br / 1000)} kbps")
    return meta, lyrics, "  \u00b7  ".join(parts) if parts else ""


# ----------------------------------------------------------------------- optional libraries
_LIB_PURPOSE = {"mutagen": "MP3/FLAC/M4A tags", "olefile": "old .doc/.ppt files",
                "xlrd": "old .xls files", "pypdf": "PDF text"}
_have_cache = {}


def _have(mod):
    if mod not in _have_cache:
        try:
            __import__(mod)
            _have_cache[mod] = True
        except ImportError:
            _have_cache[mod] = False
    return _have_cache[mod]


def _needs(ext):
    if ext in AUDIO_EXTS or ext in VIDEO_TAG_EXTS:
        return ("mutagen",)
    if ext in (".doc", ".dot", ".ppt", ".pps", ".pot"):
        return ("olefile",)
    if ext in (".xls", ".xlt"):
        return ("olefile", "xlrd")
    if ext in PDF_EXTS:
        return ("pypdf",)
    return ()


def available(ext):
    """False when the library needed for this file type is not installed."""
    return all(_have(m) for m in _needs(ext))


def missing_libs():
    """[(module, what it is needed for)] for optional libraries that are not installed."""
    return [(m, p) for m, p in _LIB_PURPOSE.items() if not _have(m)]


# ----------------------------------------------------------------------- entry point
def extract(path, ext, max_chars):
    """Return (meta, content, info) for a file; empty strings when unsupported/unreadable."""
    ext = ext.lower()
    meta = body = info = ""
    try:
        if ext in AUDIO_EXTS or ext in VIDEO_TAG_EXTS:
            meta, body, info = _audio_extract(path, ext)
        elif ext in TEXT_EXTS:
            body = _read_text(path, max_chars)
        elif ext in HTML_EXTS:
            body = _strip_html(_read_text(path, max_chars))
        elif ext in OFFICE_X:
            meta, body = _office_x(path, ext, max_chars)
        elif ext in OLE_EXTS:
            meta, body = _ole_extract(path, ext, max_chars)
        elif ext in PDF_EXTS:
            meta, body = _pdf_extract(path, max_chars)
        elif ext in RTF_EXTS:
            meta, body = _rtf_text(path, max_chars)
        elif ext in EPUB_EXTS:
            meta, body = _epub_text(path, max_chars)
    except Exception:
        pass
    return _clean(meta, 2000), _clean(body, max_chars), info
