"""Per-install identity (instance_id.py) — several copies of claudegram, each in its own
directory driving its own Telegram bot, need differentiable tray windows. The pure logic
behind that (the single-instance key, the label/color/glyph, the .desktop slug) is tested
here deps-free. gui.py just wraps the color/glyph in Qt; keeping the logic pure is what lets
these run in the offline suite AND lets install-autostart.sh mirror it in shell.
"""

import instance_id as i


# --- the single-instance KEY: the actual bug fix -------------------------------------
def test_instance_key_is_per_directory_not_global():
    # THE fix: a fixed key made a 2nd copy poke the 1st tray and exit. The key must differ
    # per install dir (so copies launch) but be stable for one dir (still one tray per install).
    a = i.instance_key("/home/x/claudegram")
    b = i.instance_key("/home/x/claudegram-work")
    assert a != b                                   # different dirs -> independent trays
    assert a == i.instance_key("/home/x/claudegram")  # same dir -> same lock (still single)
    assert a.startswith("claudegram-gui-")          # namespaced, so it can't collide globally


# --- label from directory basename ---------------------------------------------------
def test_label_from_dir_strips_redundant_prefix():
    assert i.label_from_dir("claudegram") == "claudegram"       # canonical stays put
    assert i.label_from_dir("claudegram-work") == "work"
    assert i.label_from_dir("claudegram_alice") == "alice"
    assert i.label_from_dir("claudegram.bravo") == "bravo"
    assert i.label_from_dir("cg-thing") == "cg-thing"           # unrelated prefix untouched
    assert i.label_from_dir("claudegram-") == "claudegram-"     # nothing after -> unchanged


# --- optional instance.txt overrides -------------------------------------------------
def test_parse_instance_file():
    assert i.parse_instance_file("") == (None, None, None)
    assert i.parse_instance_file(None) == (None, None, None)
    assert i.parse_instance_file("Work Bot") == ("Work Bot", None, None)
    # name, then a hex color, then a glyph — order-independent for color vs glyph line roles
    assert i.parse_instance_file("Alice\n#ff0088\n🅰") == ("Alice", "#ff0088", "🅰")
    assert i.parse_instance_file("Beta\n#abc") == ("Beta", "#abc", None)   # short hex ok
    assert i.parse_instance_file("  Padded  \n\n  X  ") == ("Padded", None, "X")  # trims/skips blanks
    # a non-hex second line is a glyph, not a color
    assert i.parse_instance_file("Gamma\nnothex") == ("Gamma", None, "nothex")


# --- the canonical install stays untouched -------------------------------------------
def test_is_default_install():
    assert i.is_default_install("claudegram", has_instance_file=False) is True
    assert i.is_default_install("claudegram", has_instance_file=True) is False   # opted in
    assert i.is_default_install("claudegram-work", has_instance_file=False) is False


# --- auto tray color -----------------------------------------------------------------
def test_accent_hsv_is_deterministic_and_spread():
    assert i.accent_hsv("work") == i.accent_hsv("work")        # stable across calls
    h, s, v = i.accent_hsv("work")
    assert 0 <= h < 360 and s == 175 and v == 190
    # different labels almost always land on different hues (this handful must)
    hues = {i.accent_hsv(n)[0] for n in ("work", "alice", "prod", "staging", "test")}
    assert len(hues) >= 4


# --- badge glyph ---------------------------------------------------------------------
def test_badge_glyph():
    assert i.badge_glyph("work") == "W"
    assert i.badge_glyph("alice") == "A"
    assert i.badge_glyph("123abc") == "1"
    assert i.badge_glyph("work", explicit="🅰") == "🅰"          # explicit wins
    assert i.badge_glyph("") == "C"                             # fallback
    assert i.badge_glyph("-_-") == "C"                          # no alnum -> fallback


# --- filesystem-safe slug + .desktop base name ---------------------------------------
def test_slug():
    assert i.slug("work") == "work"
    assert i.slug("My Bot!") == "my-bot"
    assert i.slug("a  b__c") == "a-b-c"
    assert i.slug("") == "claudegram"
    assert i.slug("!!!") == "claudegram"


def test_desktop_name():
    assert i.desktop_name("claudegram", "claudegram", has_instance_file=False) == "claudegram"
    assert i.desktop_name("claudegram-work", "work", has_instance_file=False) == "claudegram-work"
    assert i.desktop_name("claudegram", "Prod", has_instance_file=True) == "claudegram-prod"
