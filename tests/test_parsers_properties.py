"""Property-based tests (Hypothesis) for the pure parsers/formatters.

Covers invariants that the real implementations actually guarantee:
- ``markdown_to_jira_wiki`` (src/mm_jira_bot/postmortem.py)
- ``alert_signature`` / ``is_resolved_alert`` (src/mm_jira_bot/formatting.py)
- ``parse_post_id_from_text`` (src/mm_jira_bot/service/_shared.py)
- ``_csv_env`` (src/mm_jira_bot/config.py)

Strategies are structured (token lists, not free-form text) so they cannot emit
the genuine counterexamples each function does NOT promise to handle; inline notes
record where an assumed invariant only holds in a restricted form.
"""

from __future__ import annotations

import os
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from mm_jira_bot.config import _csv_env
from mm_jira_bot.formatting import alert_signature, is_resolved_alert
from mm_jira_bot.postmortem import markdown_to_jira_wiki
from mm_jira_bot.service._shared import parse_post_id_from_text

MAX = 60

# Plain words used to build markup: no "*", "@", "[", "]", "#", "(", ")" so that
# composing fragments never produces accidental markup. A bold body free of "*"
# keeps markdown_to_jira_wiki idempotent (``***x***`` would peel one "**" per pass).
_word = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
    min_size=1,
    max_size=8,
)
_handle = st.from_regex(r"[a-z][a-z0-9]{1,7}", fullmatch=True)


# --- markdown_to_jira_wiki: idempotency + email is never a mention ----------


@st.composite
def _md_fragment(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["plain", "bold", "bullet", "heading", "link", "mention"]))
    if kind == "plain":
        return draw(_word)
    if kind == "bold":
        return f"**{draw(_word)}**"
    if kind == "bullet":
        return draw(st.sampled_from(["- ", "+ "])) + draw(_word)
    if kind == "heading":
        return "#" * draw(st.integers(min_value=1, max_value=6)) + f" {draw(_word)}"
    if kind == "link":
        return f"[{draw(_word)}](https://example.com/{draw(_handle)})"
    return f"@{draw(_handle)}"


_md_text = st.lists(_md_fragment(), min_size=0, max_size=6).map("\n".join)


@settings(max_examples=200)
@given(text=_md_text)
def test_markdown_to_jira_wiki_is_idempotent(text: str) -> None:
    once = markdown_to_jira_wiki(text)
    assert markdown_to_jira_wiki(once) == once


@settings(max_examples=MAX)
@given(
    local=st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True),
    host=st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True),
    tld=st.sampled_from(["com", "ru", "org", "io", "net"]),
    prefix=st.sampled_from(["", "contact ", "email: ", "see "]),
)
def test_markdown_to_jira_wiki_never_mentions_email(
    local: str, host: str, tld: str, prefix: str
) -> None:
    # The mention rule's look-behind ``(?<![\w.@])`` excludes the ``@`` following
    # an email local-part, so ``user@host.tld`` is never turned into ``[~...]``.
    email = f"{local}@{host}.{tld}"
    out = markdown_to_jira_wiki(f"{prefix}{email}")
    assert email in out
    assert "[~" not in out


# --- alert_signature: invariant to leading noise + internal whitespace ------

# ``extract_alert_title`` -> ``truncate_for_summary`` collapses internal
# whitespace and strips ONLY leading decorative symbols / emoji shortcodes;
# trailing markup is NOT stripped. So assertions cover leading noise +
# surrounding/internal whitespace only.
_title = st.lists(_word, min_size=1, max_size=4).map(" ".join)


@settings(max_examples=MAX)
@given(title=_title)
def test_alert_signature_invariant_to_surrounding_whitespace(title: str) -> None:
    base = alert_signature(title)
    assert alert_signature(f"   {title}") == base
    assert alert_signature(f"{title}   ") == base
    assert alert_signature(f"  {title}  ") == base


@settings(max_examples=MAX)
@given(
    title=_title,
    marker=st.sampled_from(["🔴", "✅", "- ", "**", "## ", "🔴 ", ":red_circle: "]),
)
def test_alert_signature_invariant_to_leading_marker(title: str, marker: str) -> None:
    assert alert_signature(f"{marker}{title}") == alert_signature(title)


@settings(max_examples=MAX)
@given(title=_title, gap=st.integers(min_value=2, max_value=5))
def test_alert_signature_collapses_internal_whitespace(title: str, gap: int) -> None:
    spaced = title.replace(" ", " " * gap)
    assert alert_signature(spaced) == alert_signature(title)


# --- parse_post_id_from_text: round-trip + never garbage --------------------

_id_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
_VALID_ID = re.compile(r"^[a-z0-9]{20,32}$")


@settings(max_examples=MAX)
@given(
    post_id=st.text(alphabet=_id_alphabet, min_size=26, max_size=26),
    base=st.sampled_from(["https://mm.example", "https://mm.example/team", ""]),
    redirect=st.booleans(),
)
def test_parse_post_id_round_trips_permalink(post_id: str, base: str, redirect: bool) -> None:
    path = "/_redirect/pl/" if redirect else "/pl/"
    assert parse_post_id_from_text(f"{base}{path}{post_id}") == post_id


@settings(max_examples=MAX)
@given(
    post_id=st.text(alphabet=_id_alphabet, min_size=20, max_size=32),
    pad=st.text(alphabet=" \t\n", max_size=4),
)
def test_parse_post_id_round_trips_bare_id(post_id: str, pad: str) -> None:
    assert parse_post_id_from_text(f"{pad}{post_id}{pad}") == post_id


@settings(max_examples=200)
@given(text=st.text(max_size=80))
def test_parse_post_id_returns_none_or_valid_never_garbage(text: str) -> None:
    # Arbitrary text may legitimately contain a permalink id or be a bare id;
    # the result is therefore either None or a well-formed id, never garbage.
    result = parse_post_id_from_text(text)
    assert result is None or _VALID_ID.match(result) is not None


# --- is_resolved_alert: marker on first non-empty line only -----------------

_RESOLVED_MARKER = st.sampled_from(["✅", ":white_check_mark:"])
# Filler lines guaranteed to contain NO marker: plain letters + spaces only
# (no "✅", no ":" so ":white_check_mark:" cannot accidentally appear).
_clean_line = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=24)


@settings(max_examples=MAX)
@given(
    marker=_RESOLVED_MARKER,
    title=_clean_line,
    wrap=st.sampled_from(["{m} {t}", "**{m} {t}**", "{t} {m}", "  {m}{t}", "{t} {m} done"]),
    blank_prefix=st.integers(min_value=0, max_value=3),
)
def test_is_resolved_alert_true_when_marker_on_first_nonempty_line(
    marker: str, title: str, wrap: str, blank_prefix: int
) -> None:
    # Leading blank lines are skipped; a marker anywhere on the first non-empty
    # line (even wrapped in markdown/text) -> True.
    first_line = wrap.format(m=marker, t=title)
    message = "\n" * blank_prefix + first_line + "\nsome later body text"
    assert is_resolved_alert(message) is True


@settings(max_examples=MAX)
@given(marker=_RESOLVED_MARKER, first=_clean_line, later=_clean_line)
def test_is_resolved_alert_false_when_marker_only_on_later_line(
    marker: str, first: str, later: str
) -> None:
    # First non-empty line is clean; a marker on a later line is ignored -> False.
    message = f"\n\nfiring {first} title\nmore text\n{marker} {later}"
    assert is_resolved_alert(message) is False


# --- _csv_env: trimmed, non-empty, leading-@ stripped -----------------------

# Item bodies that survive ``.strip().lstrip("@")`` as non-empty; free of the
# "," / ";" separators and of leading whitespace/"@" of their own.
_csv_item = st.from_regex(r"[a-z][a-z0-9_.\-]{0,7}", fullmatch=True)


@settings(max_examples=MAX)
@given(
    items=st.lists(_csv_item, min_size=1, max_size=6),
    seps=st.lists(st.sampled_from([",", ";"]), min_size=6, max_size=6),
    at_prefix=st.booleans(),
    inject_empty=st.booleans(),
)
def test_csv_env_yields_clean_items(
    items: list[str], seps: list[str], at_prefix: bool, inject_empty: bool
) -> None:
    # Whitespace pads only AROUND whole items (never between "@" and the name,
    # which .strip().lstrip("@") would not clean). Optional empty segments
    # exercise the empty-entry filter.
    parts: list[str] = []
    for index, item in enumerate(items):
        decorated = ("@" if at_prefix else "") + item
        parts.append(f"  {decorated}\t")
        if inject_empty and index % 2 == 0:
            parts.append("   ")
    raw = parts[0]
    for index, part in enumerate(parts[1:]):
        raw += seps[index % len(seps)] + part

    # Set env directly (not the monkeypatch fixture: function-scoped fixtures
    # don't reset per Hypothesis example and trip a HealthCheck).
    name = "MM_JIRA_TEST_CSV_ENV"
    os.environ[name] = raw
    try:
        result = _csv_env(name)
    finally:
        os.environ.pop(name, None)

    assert result == tuple(items)
    for entry in result:
        assert entry != ""
        assert not entry.startswith("@")
        assert entry == entry.strip()


@settings(max_examples=MAX)
@given(value=st.none() | st.just(""))
def test_csv_env_empty_or_unset_is_empty_tuple(value: str | None) -> None:
    name = "MM_JIRA_TEST_CSV_ENV_EMPTY"
    os.environ.pop(name, None)
    if value is not None:
        os.environ[name] = value
    try:
        assert _csv_env(name) == ()
    finally:
        os.environ.pop(name, None)
