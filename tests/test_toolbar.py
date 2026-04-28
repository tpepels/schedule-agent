from __future__ import annotations

from schedule_agent.cli import (
    _TOOLBAR_ITEMS,
    _toolbar_tier,
    build_toolbar_fragments,
)


def _text(fragments: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in fragments)


def _key_fragments(fragments: list[tuple[str, str]]) -> list[str]:
    return [text for style, text in fragments if "key" in style]


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


def test_tier_very_narrow():
    assert _toolbar_tier(40) == "very_narrow"


def test_tier_narrow():
    assert _toolbar_tier(80) == "narrow"


def test_tier_normal():
    assert _toolbar_tier(120) == "normal"


def test_tier_wide():
    assert _toolbar_tier(180) == "wide"


# ---------------------------------------------------------------------------
# Wide tier
# ---------------------------------------------------------------------------


def test_wide_uses_full_labels():
    text = _text(build_toolbar_fragments(180))
    # full / verbose labels appear verbatim
    for word in ("Add", "Now", "eschedule", "Edit", "Unschedule", "Delete", "Quit"):
        assert word in text


def test_wide_single_line():
    text = _text(build_toolbar_fragments(180))
    # at most one trailing newline
    assert text.count("\n") <= 1


# ---------------------------------------------------------------------------
# Normal tier
# ---------------------------------------------------------------------------


def test_normal_uses_compact_labels():
    text = _text(build_toolbar_fragments(120))
    assert "Resch" in text
    assert "Unsched" in text
    assert "Del" in text
    # full forms should not appear in normal tier
    assert "Reschedule" not in text
    assert "Unschedule" not in text
    assert "Delete" not in text


def test_normal_single_line():
    text = _text(build_toolbar_fragments(120))
    assert text.count("\n") <= 1


# ---------------------------------------------------------------------------
# Narrow tier
# ---------------------------------------------------------------------------


def test_narrow_key_first_format():
    fragments = build_toolbar_fragments(80)
    text = _text(fragments)
    # key-first style: "A Add", "N Now", etc.
    assert "A" in _key_fragments(fragments)
    assert "Add" in text
    assert "Now" in text
    assert "Resch" in text


def test_narrow_drops_low_priority_actions():
    text = _text(build_toolbar_fragments(80))
    # Prefix/Session/Unsched/Filter/Scope are not in the narrow subset
    assert "Prefix" not in text
    assert "Session" not in text
    assert "Unsched" not in text
    assert "Filter" not in text
    assert "Scope" not in text


def test_narrow_keeps_high_priority_actions():
    text = _text(build_toolbar_fragments(80))
    # Quit, help, search, submit, delete are always shown
    assert "Quit" in text
    assert "Help" in text
    assert "Search" in text
    assert "Submit" in text
    assert "Del" in text


def test_narrow_single_line():
    text = _text(build_toolbar_fragments(80))
    assert text.count("\n") <= 1


# ---------------------------------------------------------------------------
# Very narrow tier
# ---------------------------------------------------------------------------


def test_very_narrow_keys_only():
    fragments = build_toolbar_fragments(40)
    text = _text(fragments)
    keys = _key_fragments(fragments)
    # Only single-character keys, no full labels
    assert "Add" not in text
    assert "Quit" not in text
    assert "Help" not in text
    # high-priority keys present
    assert "A" in keys
    assert "Q" in keys
    assert "?" in keys
    assert "/" in keys
    assert "S" in keys
    assert "D" in keys


def test_very_narrow_single_line():
    fragments = build_toolbar_fragments(40)
    text = _text(fragments)
    assert text.count("\n") <= 1


# ---------------------------------------------------------------------------
# Action mapping invariant
# ---------------------------------------------------------------------------


def test_all_tiers_show_subset_of_known_keys():
    # Every "class:key" fragment in any tier must correspond to a known
    # action key. This protects against silently dropping a binding by
    # mis-typing a key in _TOOLBAR_ITEMS.
    known = {item[0] for item in _TOOLBAR_ITEMS}
    for width in (40, 80, 120, 180):
        keys = _key_fragments(build_toolbar_fragments(width))
        for key in keys:
            assert key in known, f"unknown key {key!r} at width={width}"


def test_narrow_subset_matches_visible_flag():
    # narrow tier must show exactly the items flagged narrow_visible=True
    expected = [item[0] for item in _TOOLBAR_ITEMS if item[3]]
    keys = _key_fragments(build_toolbar_fragments(80))
    assert keys == expected


def test_very_narrow_subset_matches_visible_flag():
    expected = [item[0] for item in _TOOLBAR_ITEMS if item[3]]
    keys = _key_fragments(build_toolbar_fragments(40))
    assert keys == expected


def test_wide_shows_every_known_action():
    keys = _key_fragments(build_toolbar_fragments(180))
    for item in _TOOLBAR_ITEMS:
        assert item[0] in keys, f"missing {item[0]!r} in wide tier"
