# DeskSearch

**Tap Ctrl twice, type, press Enter.** A fast, private file search for Windows that finds files by
name, by the words inside them, and by music tags, across every drive you choose.

In the spirit of the old Google Desktop: a small popup that appears from anywhere, answers as you type,
and gets out of your way. Everything stays on your PC, and nothing is sent anywhere.

```
  ┌───────────────────────────────────────────────────────────────┐
  │  invoce 2024                                                  │
  │  [All] Documents  Music  Pictures  Videos  Apps  Code         │
  ├───────────────────────────────────────────────────────────────┤
  │  📄 Invoice_March_2024.pdf                 name · contents    │
  │     D:\Accounts\2024   …total due for the invoice period…     │
  │  📊 invoices-2024.xlsx                     name               │
  │  📄 GST return notes.docx                  contents           │
  └───────────────────────────────────────────────────────────────┘
     (Typos are forgiven: "invoce" still finds "invoice".)
```

---

## Why use it instead of Windows Search?

Windows Search is built into the system and covers the basics. DeskSearch is built for people who search
their own files many times a day and want an answer *now*, from *any* drive, even when they don't remember
the exact name.

| | **DeskSearch** | **Windows Search** |
|---|---|---|
| **Open it** | Tap **Ctrl twice** from any app | Start menu or Explorer search box |
| **Speed** | Results as you type: 20–50 ms on a 400,000-file index | Start menu search can lag, and mixes in Bing web results |
| **Typos** | `invoce` finds *invoice*; `port` finds *report* | Mostly exact words or word starts |
| **Which drives** | Any drives and folders you add, such as all of `C:\` to `G:\` | User folders by default. "Enhanced" mode covers more but is heavy |
| **File patterns** | `*.pdf`, `report*.docx`, `IMG_????.jpg`, `*.{pdf,docx}` | Partial support in Explorer, awkward to type |
| **Filters** | `size:>5mb`, `modified:week`, `in:invoices`, `type:music`, `-draft` | Similar AQS filters in Explorer, long to type |
| **Music** | Search by title, artist, album, year or lyrics. Results show `Artist — Album · 3:45 · 320 kbps` | Properties are indexed, but shown in a plain file list |
| **Ranking** | Learns which files *you* open, and favours recent and well-named matches | Generic ranking |
| **Why it matched** | Every result says: *name*, *tags*, *contents*, *folder* or *similar name* | Not shown |
| **Control** | You choose exactly which file types get their text indexed | Mostly fixed by the system |
| **Privacy** | 100% local, no web results, no telemetry | Start search sends queries to Bing unless that is turned off |

**When Windows Search is the better choice:** if you mostly search Outlook email or OneNote, or need
formats that only an app's own plug-in can read. Windows Search covers those, and DeskSearch does not.
The two run side by side without conflict.

---

## Features

- **Global hotkey:** double-tap Ctrl anywhere to open the search popup. Esc hides it.
- **Searches inside documents:** Word (`doc`, `docx`, `rtf`, `odt`), Excel (`xls`, `xlsx`, `ods`),
  text-based PDF and `.txt`. Matching text is shown under the file name with your words highlighted.
- **Music and video tags:** MP3, FLAC, M4A, OGG, OPUS, WMA, WAV, AIFF, MP4 and more.
- **Every other file** (images, videos, zip, exe, csv, code...) is found by name and folder.
- **Typo-tolerant and partial-word matching** on file names.
- **Smart queries:** file patterns, exact phrases, exclusions and filters (see below).
- **Category chips:** All / Documents / Music / Pictures / Videos / Apps / Code. Tab or Ctrl+1..7 switches.
- **Live updates:** new, edited, renamed and deleted files show up within seconds.
- **Incremental index:** built once, then only changed files are read again.
- **Light and dark themes**, high-DPI aware. Optional start with Windows.

### Keyboard

| Key | Action |
|---|---|
| Ctrl, Ctrl | Show / hide the popup |
| Enter | Open the file |
| Ctrl+Enter | Show the file in Explorer |
| Ctrl+Shift+C | Copy the file's path (also on right-click) |
| Tab, Ctrl+1..7 | Switch category |
| ↑ ↓ | Move through results. **Show more** loads the next page |
| Esc | Close |

---

## Install

### Option A: download the app (no Python needed)

1. Download `DeskSearch.exe` from the [Releases](https://github.com/nrbhai/desksearch/releases) page.
2. Run it. A magnifier icon appears in the system tray, near the clock.
3. In the settings window, add the folders or drives to index, such as `D:\` or your Documents and Music folders.
4. Tap **Ctrl twice** and start typing.

> Windows SmartScreen or some antivirus tools may warn about an unsigned app that uses a keyboard
> hook (needed for the double-Ctrl shortcut). If you prefer, use Option B, which runs from the source.

### Option B: run from source

1. Install [Python 3.10+](https://www.python.org/downloads/) and tick **"Add Python to PATH"**.
2. Clone or download this repository.
3. Double-click `run.bat`. The first run installs the libraries, then starts DeskSearch in the tray.

---

## How it understands what you type

DeskSearch tells apart *"I want files of this kind"* and *"I want this word, in a name or inside a file"*.
The bottom of the popup always shows how it read your query.

| You type | Meaning | Matches |
|---|---|---|
| `*.pdf`   `*.exe`   `.mp3` | a file type | only files with exactly that extension |
| `report*.docx`   `IMG_????.jpg`   `*2024*` | a name pattern (`*` = any text, `?` = one character) | file names only |
| `*.{pdf,docx}` | several types | those extensions only |
| `invoice.pdf` | a file name | names like `*invoice*.pdf` |
| `invoice`   `tax return` | words | a file name, a tag, or text inside a file |
| `*.pdf invoice` | pattern + words | PDFs that mention *invoice* (in the name or text) |
| `"annual report"` | exact phrase | those words in that order |
| `-draft` | exclude | files without that word |
| `name:budget` / `content:budget` | names only / text only | |
| `type:music`, `in:invoices`, `size:>5mb`, `modified:week`, `modified:>2025-01-31` | filters | |

`type:` accepts documents, music, pictures, videos, apps, code, sheets, slides, pdf and archives.

Results are ranked by how well the name matches, where the words matched (name > tags > folder > contents),
how recently the file changed, and how often you open it. With an empty search box, the popup shows the
files you opened recently.

---

## How the index works

- The index is a local SQLite database in `%APPDATA%\DeskSearch\index.db`.
- **It is built once.** After that, a file is read again only when its size or modified time changes.
- **Live:** a file system watcher applies changes within a couple of seconds.
- **Safety net:** a full re-check runs every 60 minutes by default (adjustable in Settings). It only compares
  sizes and dates, and the status shows *"Up to date · checking for changes"* while it runs.
- **Restart-friendly:** restarting soon after a finished scan does not walk every drive again.
- **Unplugged drives** keep their entries until the drive comes back.
- **Removed folders**, or folders added to the skip list, are dropped from the index.

### Choosing which file types are read inside

The `content_exts` list in `%APPDATA%\DeskSearch\settings.json` decides which types get their text indexed.
The default is Word, Excel, PDF and `.txt`, plus music and video tags. DeskSearch can also read PowerPoint,
EPUB, Markdown, CSV, HTML and source code: add for example `".pptx"` or `".md"` to the list. Remove a type, and
the next scan drops its stored text without reading any file again. Quit DeskSearch before editing the file.

Folders named `Windows`, `Program Files`, `ProgramData`, `AppData`, `node_modules`, `.git` and similar are
skipped by default (Settings > folder names to skip).

---

## Build the .exe yourself

1. Quit DeskSearch (tray icon > Quit).
2. Raise `VERSION` in `version.py` (1.0 → 1.1 → 1.2 …) and add a line to `CHANGELOG.md`.
3. Double-click `build.bat`. It creates `dist\DeskSearch.exe` and keeps a copy as
   `releases\DeskSearch-<version>.exe`.

The version is shown in the tray menu, in the settings window title and in the exe's Properties > Details.

---

## Known limits

- Scanned PDFs (pictures of pages) contain no text to index. Password-protected files are found by name only.
- No Outlook email or OneNote search.
- After the app was closed for a while, it catches up by walking the indexed folders. On a large
  spinning hard disk this takes a few minutes, while search keeps working.
- Apps running as administrator only see the double-Ctrl shortcut if DeskSearch also runs as administrator.
- Windows only.

## Project layout

| File | Purpose |
|---|---|
| `app.py` | Popup UI, tray icon, settings window |
| `engine.py` | Index (SQLite FTS5 + trigram), incremental scanner, file watcher, search and ranking |
| `extractors.py` | Text and tag extraction for documents, PDF and audio |
| `hotkey.py` | Double-Ctrl detection |
| `config.py` | Settings in `%APPDATA%\DeskSearch\settings.json` |
| `version.py` | App version |

## License

[GPL-2.0](LICENSE)
