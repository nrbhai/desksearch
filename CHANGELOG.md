# DeskSearch changelog

Version lives in `version.py`. Each new build gets the next number (1.1, 1.2, ...) and a line here.

## 1.0 — 2026-11-25

First numbered release.

- Word, Excel, PDF and .txt are searchable by the words inside them; music/video by their tags.
  Everything else (csv, json, html, code, ...) is found by file name and folder only.
  The list is `content_exts` in settings.json. The index went from 12 GB to 2 GB.
- A routine re-check of an already built index shows "Up to date · checking for changes"
  instead of "Indexing…". "Indexing" now only means the first build.
- Restarting soon after a finished scan no longer walks every drive again.
- The version shows in the tray menu, tray tooltip, settings title and the .exe's file properties.
