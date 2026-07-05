"""Per-install identity for claudegram.

You can run several copies of the whole install — each in its OWN directory, each with its
own token.txt (so its own Telegram bot) and its own tray window. For that to work the tray
must be (a) launchable more than once and (b) visually distinguishable. This module holds the
tiny bit of logic behind both, kept PURE and dependency-free (no Qt) so it's unit-testable and
so install-autostart.sh can mirror it in shell:

  - instance_key(dir)      -> the single-instance lock key, unique PER DIRECTORY. THE fix: the
                              old fixed key meant a 2nd copy just poked the 1st tray and exited.
  - label_from_dir(name)   -> a friendly label from the directory basename
  - parse_instance_file()  -> optional instance.txt overrides (name / accent color / glyph)
  - is_default_install()   -> the canonical lone 'claudegram' install (keeps its original look)
  - accent_hsv / badge_glyph -> the tray badge's auto color + letter for a named install
  - slug / desktop_name    -> a filesystem-safe id for the .desktop / WM class

gui.py wraps the color as a QColor and the glyph into a drawn icon; nothing here imports Qt.
"""

import hashlib

DEFAULT_NAME = "claudegram"
_STRIP_PREFIXES = ("claudegram-", "claudegram_", "claudegram.")


def label_from_dir(dir_name: str) -> str:
    """Display label derived from the install's directory basename. A redundant 'claudegram-'
    prefix is stripped so a copy in `claudegram-work` reads as just 'work'; the canonical
    `claudegram` directory stays 'claudegram'."""
    low = dir_name.lower()
    for pre in _STRIP_PREFIXES:
        if low.startswith(pre) and len(dir_name) > len(pre):
            return dir_name[len(pre):]
    return dir_name


def parse_instance_file(text: str | None):
    """Optional `instance.txt` overrides -> (name, color_hex, glyph), each None if absent.
    First non-empty line = display name; any later line that looks like '#rgb'/'#rrggbb' sets
    the accent color; the next non-empty line after the name sets an explicit badge glyph
    (e.g. an emoji). Blank/missing file -> all None (fall back to directory-derived defaults)."""
    name = color = glyph = None
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if name is None:
            name = ln
        elif color is None and ln.startswith("#") and len(ln) in (4, 7):
            color = ln
        elif glyph is None:
            glyph = ln
    return name, color, glyph


def is_default_install(dir_name: str, has_instance_file: bool) -> bool:
    """True only for the canonical single install: directory 'claudegram' with no instance.txt.
    That case keeps the original tray look/title untouched — differentiation only kicks in once
    you actually make a second, differently-named copy."""
    return dir_name == DEFAULT_NAME and not has_instance_file


def instance_key(dir_path: str) -> str:
    """Single-instance lock key, unique per INSTALL DIRECTORY (a hash of its absolute path).
    This is the fix that lets a copy in another directory launch its own tray instead of
    connecting to — and merely focusing — the first one. Same dir -> same key (still exactly
    one tray per install); different dir -> different key (independent trays)."""
    return "claudegram-gui-" + hashlib.sha1(dir_path.encode("utf-8")).hexdigest()[:12]


def accent_hsv(name: str) -> tuple[int, int, int]:
    """A deterministic, well-spread (hue, sat, val) for a label — so different installs get
    different tray colors with zero configuration. FNV-1a over the label picks the hue."""
    h = 2166136261
    for ch in name:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return (h % 360, 175, 190)


def badge_glyph(name: str, explicit: str | None = None) -> str:
    """The glyph drawn on a named install's tray badge: an explicit override (e.g. an emoji)
    if given, else the first alphanumeric char of the label, uppercased. Falls back to 'C'."""
    if explicit:
        return explicit
    for ch in name:
        if ch.isalnum():
            return ch.upper()
    return "C"


def slug(name: str) -> str:
    """A filesystem/WM-safe slug: lowercase, non-alphanumerics collapsed to single dashes."""
    out = "".join(ch if ch.isalnum() else "-" for ch in name.lower())
    out = "-".join(p for p in out.split("-") if p)
    return out or DEFAULT_NAME


def desktop_name(dir_name: str, label: str, has_instance_file: bool) -> str:
    """Base name for this install's autostart .desktop file / WM class. The canonical install
    keeps 'claudegram'; every other copy gets 'claudegram-<slug>' so their entries and taskbar
    groups don't collide."""
    if is_default_install(dir_name, has_instance_file):
        return DEFAULT_NAME
    return "claudegram-" + slug(label)
