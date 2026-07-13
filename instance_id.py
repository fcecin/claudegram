"""Per-install identity for claudegram.

You can run several copies of the whole install — each in its OWN directory, each with its
own token.txt (so its own Telegram bot) and its own tray window. For that to work the tray
must be (a) launchable more than once and (b) visually distinguishable. This module holds the
tiny bit of logic behind both, kept PURE and dependency-free (no Qt) so it's unit-testable and
so the shell scripts can reuse it via the `__main__` CLI (rather than re-implementing it):

  - instance_key(dir)      -> the single-instance lock key, unique PER DIRECTORY. THE fix: the
                              old fixed key meant a 2nd copy just poked the 1st tray and exited.
  - label_from_dir(name)   -> a friendly label from the directory basename
  - parse_instance_json()  -> the DECLARED identity from instance.json (name / color / glyph)
  - parse_instance_file()  -> legacy instance.txt fallback (name / accent color / glyph)
  - resolve(dir, ...)      -> the single resolver: (label, color, glyph, is_default)
  - is_default_install()   -> the canonical lone 'claudegram' install (keeps its original look)
  - accent_hsv / badge_glyph -> the tray badge's auto color + letter for a named install
  - slug / desktop_name    -> a filesystem-safe id for the .desktop / WM class

Identity precedence: instance.json (declared) > instance.txt (legacy) > directory basename
(foolproof fallback). gui.py wraps the color as a QColor and the glyph into a drawn icon; the
`__main__` CLI at the bottom lets the shell scripts reuse this instead of mirroring it — so this
module is the SINGLE source of truth. Nothing here imports Qt.
"""

import hashlib
import json

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


def parse_instance_json(text: str | None):
    """The DECLARED identity from instance.json -> (name, color, glyph), each None if absent or
    the file is malformed. Shape: {"name": "research", "color": "#c2410c", "glyph": "2"}. A bad
    file degrades to the directory-name fallback rather than crashing the tray."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return (None, None, None)
    if not isinstance(data, dict):
        return (None, None, None)

    def field(key):
        v = data.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    return (field("name"), field("color"), field("glyph"))


def parse_allowed_ids(text: str | None) -> list[int]:
    """Authorized Telegram user ids from instance.json's "allowed_user_ids" (a list). FILE ORDER
    matters: the first id is the MASTER (gets the proactive notifications); the rest are guests.
    Deduplicated, order preserved; non-int entries skipped. Missing/malformed -> []. This is
    per-install config read from the FILE, never the environment (which leaks between installs)."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    ids: list[int] = []
    for v in data.get("allowed_user_ids") or []:
        try:
            iv = int(v)
        except (ValueError, TypeError):
            continue
        if iv not in ids:
            ids.append(iv)
    return ids


def is_default_install(dir_name: str, has_identity_file: bool) -> bool:
    """True only for the canonical single install: directory 'claudegram' with no declared
    identity (no instance.json / instance.txt). That case keeps the original tray look/title
    untouched — differentiation only kicks in once you make a second, differently-named copy."""
    return dir_name == DEFAULT_NAME and not has_identity_file


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


def desktop_name(dir_name: str, label: str, has_identity_file: bool) -> str:
    """Base name for this install's autostart .desktop file / WM class. The canonical install
    keeps 'claudegram'; every other copy gets 'claudegram-<slug>' so their entries and taskbar
    groups don't collide."""
    if is_default_install(dir_name, has_identity_file):
        return DEFAULT_NAME
    return "claudegram-" + slug(label)


def resolve(dir_name: str, json_text=None, txt_text=None):
    """THE resolver: fold the identity precedence (instance.json > instance.txt > directory
    basename) into (label, color, glyph, is_default). Pass the raw file contents (or None if a
    file is absent); explicit `color`/`glyph` may be None to mean 'auto-derive from the label'."""
    name = color = glyph = None
    has_file = False
    if json_text is not None:
        name, color, glyph = parse_instance_json(json_text)
        has_file = True
    elif txt_text is not None:
        name, color, glyph = parse_instance_file(txt_text)
        has_file = True
    label = name or label_from_dir(dir_name)
    return label, color, glyph, is_default_install(dir_name, has_file)


if __name__ == "__main__":
    # Single source of truth for the shell scripts: instead of mirroring the naming logic in
    # bash (and drifting), install-autostart.sh calls e.g. `python3 instance_id.py desktop_name .`
    import pathlib
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "label"
    root = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
    jf, tf = root / "instance.json", root / "instance.txt"
    jtext = jf.read_text(encoding="utf-8") if jf.exists() else None
    ttext = tf.read_text(encoding="utf-8") if (jtext is None and tf.exists()) else None
    has_file = jtext is not None or ttext is not None
    label, _color, _glyph, default = resolve(root.name, jtext, ttext)
    if cmd == "label":
        print(label)
    elif cmd == "desktop_name":
        print(desktop_name(root.name, label, has_file))
    elif cmd == "title":
        print(DEFAULT_NAME if default else f"claudegram · {label}")
    elif cmd == "is_default":
        print("1" if default else "0")
    else:
        sys.exit(f"unknown command: {cmd}")
