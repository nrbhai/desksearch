"""DeskSearch version. Bump this before each build: 1.0 -> 1.1 -> 1.2 ...
Also add a line to CHANGELOG.md saying what changed."""
VERSION = "1.0"


def as_tuple():
    """'1.2' -> (1, 2, 0, 0), the 4-number form Windows file properties need."""
    nums = [int(x) for x in VERSION.split(".")][:4]
    return tuple(nums + [0] * (4 - len(nums)))


if __name__ == "__main__":
    # `python version.py` prints the version; `python version.py --rcfile out.txt` writes the
    # PyInstaller version resource (shown in the .exe's Properties > Details).
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--rcfile":
        t = as_tuple()
        rc = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'DeskSearch'),
      StringStruct('FileDescription', 'DeskSearch - local file search'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductVersion', '{VERSION}'),
      StringStruct('OriginalFilename', 'DeskSearch.exe'),
      StringStruct('InternalName', 'DeskSearch')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
        with open(sys.argv[2], "w", encoding="utf-8") as fh:
            fh.write(rc)
    else:
        print(VERSION)
