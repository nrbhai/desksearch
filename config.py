"""Settings storage for DeskSearch (JSON file in %APPDATA%\\DeskSearch)."""
import json
import os
import sys

APP_NAME = "DeskSearch"

DEFAULT_EXCLUDED_DIRS = [
    "node_modules", ".git", ".svn", "__pycache__", "$RECYCLE.BIN",
    "System Volume Information", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", "AppData", ".venv", "venv",
    ".cache",
]

# File types whose *text* is indexed (words inside). Everything else is found by name/folder only.
# Music/video tags (title, artist, album, lyrics) are always read; they are small.
DEFAULT_CONTENT_EXTS = [
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".dotm", ".rtf", ".odt",      # Word
    ".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".xltm", ".ods",              # Excel
    ".pdf",                                                                  # PDF
    ".txt",                                                                  # text
]

DEFAULTS = {
    "folders": [],
    "excluded_dirs": DEFAULT_EXCLUDED_DIRS,
    "index_contents": True,
    "content_exts": DEFAULT_CONTENT_EXTS,   # which file types get their text indexed
    "max_content_file_mb": 20,       # skip content extraction above this size
    "max_content_chars": 500_000,    # per-file cap on stored text
    "rescan_minutes": 60,            # safety-net full scan (live watcher handles changes)
    "double_tap_ms": 400,            # max gap between the two Ctrl taps
    "max_results": 50,
    "page_size": 5,                  # results shown at first; "Show more" adds this many
    "skip_hidden_folders": True,     # skip folders starting with "." (.git, .vscode, .kiro, ...)
    "theme": "light",                # light | dark
}


def data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def db_path() -> str:
    return os.path.join(data_dir(), "index.db")


def _config_file() -> str:
    return os.path.join(data_dir(), "settings.json")


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(_config_file(), "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    tmp = _config_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.replace(tmp, _config_file())


def launch_command() -> str:
    """Command line used for 'start with Windows'."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pyw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pyw):
            exe = pyw
    script = os.path.abspath(sys.argv[0])
    return f'"{exe}" "{script}"'


def set_autostart(enabled: bool) -> bool:
    """Add/remove HKCU Run entry. Returns True on success (Windows only)."""
    try:
        import winreg
    except ImportError:
        return False
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def get_autostart() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except (ImportError, OSError):
        return False
