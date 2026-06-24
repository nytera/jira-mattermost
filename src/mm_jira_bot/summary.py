from __future__ import annotations

import re

# ``@username`` mentions the LLM emits (for the Jira ``[~username]`` rendering)
# must NOT ping anyone in the Mattermost thread, so the summary path strips the
# leading ``@`` to plain text. Mirrors ``postmortem._MD_MENTION`` (emails like
# ``user@host`` are left untouched; a trailing sentence period is not swallowed).
_MENTION = re.compile(r"(?<![\w.@])@([A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)*)")


def neutralize_mentions(text: str) -> str:
    """Strip the ``@`` from ``@username`` so a thread summary never pings."""
    return _MENTION.sub(r"\1", text)


def format_thread_summary_reply(summary: str) -> str:
    return "\n".join(["📝 **Саммари треда**", "", neutralize_mentions(summary.strip())])


def format_thread_summary_streaming(partial: str) -> str:
    """In-progress render of the summary while the LLM streams it into the thread.

    The header carries a "генерируется…" marker so the partial text never reads as
    final; ``format_thread_summary_reply`` overwrites it with the clean header once
    the full text arrives.
    """
    return "\n".join(
        ["📝 **Саммари треда** _(генерируется…)_", "", neutralize_mentions(partial.strip())]
    )
