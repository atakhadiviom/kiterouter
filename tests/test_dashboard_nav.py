"""Guards the dashboard's icon rail and FEATURES registry against drift.

The rail mirrors OmniRoute's `(dashboard)/dashboard` page set, so entries are
easy to add and just as easy to lose. These checks run offline against the
static file — no server, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DASHBOARD = (
    Path(__file__).resolve().parents[1] / "src" / "kiterouter" / "static" / "dashboard.html"
)

# OmniRoute 3.8.50 `(dashboard)/dashboard` pages: every one needs a rail entry,
# so coverage is asserted rather than assumed.
OMNIROUTE_PAGES = [
    "a2a", "acp-agents", "activity", "agent-skills", "analytics", "api-endpoints",
    "api-manager", "audit", "auto-combo", "batch", "cache", "changelog", "chaos",
    "cli-agents", "cli-code", "cloud-agents", "combos", "compression", "conductor",
    "context", "conversations", "costs", "discovery", "endpoint",
    "free-provider-rankings", "free-tiers", "gamification", "health", "leaderboard",
    "limits", "logs", "mcp", "media-providers", "memory", "omni-skills", "onboarding",
    "playground", "plugins", "profile", "provider-stats", "providers", "quota",
    "radar", "relay", "resilience", "runtime", "search-tools", "settings", "system",
    "tokens", "tools", "translator", "usage", "webhooks",
]

# Every icon name used by the dashboard, validated against lucide's icon set
# (1,743 names). A wrong name renders an empty box instead of failing loudly, so
# the list is pinned: confirm any new name against lucide before adding it here.
LUCIDE_ICONS = {
    "activity", "alert-triangle", "arrow-right", "bar-chart-3", "bot",
    "brain-circuit", "chart-line", "check", "clipboard-list", "cloud", "code-2",
    "coins", "copy", "cpu", "database", "database-zap", "dollar-sign", "download",
    "download-cloud", "edit-3", "file-archive", "gamepad-2", "gauge", "gift",
    "git-branch", "git-fork", "git-merge", "graduation-cap", "heart-pulse",
    "history", "image", "key", "key-round", "languages", "layers",
    "layout-dashboard", "link", "list-checks", "list-ordered", "loader",
    "loader-2", "memory-stick", "messages-square", "monitor", "network",
    "pie-chart", "play", "play-circle", "plug", "plug-zap", "plus", "puzzle",
    "radar", "refresh-cw", "repeat", "route", "save", "scroll-text", "search",
    "search-code", "server", "server-cog", "settings", "share-2", "shield-check",
    "sparkles", "square-terminal", "telescope", "terminal", "trash-2",
    "trending-up", "triangle-alert", "trophy", "user", "wand-sparkles",
    "webhook", "wrench", "x", "zap",
}

ENTRY_SPLIT_RE = re.compile(r"\{ id:'")
SECTION_RE = re.compile(r'<section id="tab-([a-z0-9-]+)"([^>]*)>')
ICON_ATTR_RE = re.compile(r'data-lucide="([a-z0-9-]+)"')
ICON_KEY_RE = re.compile(r"icon:'([a-z0-9-]+)'")


@pytest.fixture(scope="module")
def html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def blocks(html) -> list:
    """One text block per FEATURES registry entry."""
    start = html.index("const FEATURES = [")
    end = html.index("\n    ];", start)
    return ENTRY_SPLIT_RE.split(html[start:end])[1:]


def entry_id(block: str) -> str:
    return block.split("'")[0]


def is_planned(block: str) -> bool:
    return "status:'planned'" in block


def section_ids(html: str) -> set:
    return {m.group(1) for m in SECTION_RE.finditer(html)}


def test_registry_parses_all_entries(blocks):
    assert len(blocks) == 57, "expected 57 rail entries (55 OmniRoute pages + overview + connect)"


def test_entry_ids_are_unique(blocks):
    ids = [entry_id(b) for b in blocks]
    assert len(ids) == len(set(ids)), "duplicate feature id in FEATURES"


@pytest.mark.parametrize("field", ["label:'", "icon:'", "group:'", "status:'"])
def test_every_entry_declares_required_fields(blocks, field):
    for block in blocks:
        assert field in block, "feature " + entry_id(block) + " is missing " + field


def test_status_is_live_or_planned(blocks):
    for block in blocks:
        assert "status:'live'" in block or "status:'planned'" in block


def test_live_features_have_a_real_section(html, blocks):
    present = section_ids(html)
    live = [entry_id(b) for b in blocks if not is_planned(b)]
    assert live, "expected at least one live feature"
    for feature_id in live:
        assert feature_id in present, feature_id + " is marked live but has no <section>"


def test_planned_features_have_no_static_section(html, blocks):
    """Planned panels are generated at runtime, so a static section means drift."""
    present = section_ids(html)
    for block in blocks:
        if is_planned(block):
            feature_id = entry_id(block)
            assert feature_id not in present, (
                feature_id + " is marked planned but already has a static section"
            )


def test_every_planned_feature_documents_itself(blocks):
    """A stub must say what it will do, what it is based on, and how it is done."""
    for block in blocks:
        if not is_planned(block):
            continue
        feature_id = entry_id(block)
        assert "summary:'" in block, feature_id + " has no summary"
        assert "reference:'" in block, feature_id + " has no OmniRoute reference"
        assert "checklist:" in block, feature_id + " has no checklist"


def test_rail_covers_every_omniroute_page(blocks):
    ids = {entry_id(b) for b in blocks}
    missing = [p for p in OMNIROUTE_PAGES if p not in ids]
    assert not missing, "OmniRoute features with no rail entry: " + str(missing)


def test_overview_section_is_visible_by_default(html):
    match = SECTION_RE.search(html)
    assert match is not None and match.group(1) == "overview"
    assert "hidden" not in match.group(2)


def test_rail_is_generated_from_the_registry(html):
    """The feature nav must be rendered from FEATURES, not hand-written.

    The rail footer (health link, backup button) is static by design; only the
    feature list itself is asserted here, since a hand-written copy of it is
    what would silently drift out of sync.
    """
    nav_start = html.index('<nav id="nav-rail"')
    nav = html[nav_start: html.index("</nav>", nav_start)]
    assert "switchTab(" not in nav, "rail buttons must be rendered from FEATURES"
    assert "data-lucide" not in nav, "rail icons must come from the registry"
    assert "Injected from FEATURES" in nav


def test_tooltip_layer_exists(html):
    assert 'id="rail-tooltip"' in html


def test_all_icons_are_valid_lucide_names(html):
    used = set(ICON_ATTR_RE.findall(html)) | set(ICON_KEY_RE.findall(html))
    unknown = sorted(i for i in used if i not in LUCIDE_ICONS)
    assert not unknown, "not validated against lucide: " + str(unknown)
