"""P052: canonical ``@DisplayName`` addressing shared by Owner text and authenticated Agent replies.

One deterministic rule (the frontend mirrors it in ``frontend/src/presentations/default/ui/mentions.ts`` only as a
preview; the server result is authoritative):

* ``@`` must start the text or follow a character that is not a letter, digit or ``_`` (so ``a@Claude`` is an e-mail,
  not a mention);
* the display name is compared case-insensitively with Unicode case folding, exactly (never a prefix or substring);
* the character after the name must not be a letter, digit, ``_`` or ``-`` (so ``@Claude-2`` is not ``@Claude``);
* the longest candidate name wins at one position, so ``@Claude Opus`` never also counts as ``@Claude``;
* a folded name shared by two different candidates is AMBIGUOUS and resolves to nobody.

The result is a list of candidate refs in first-mention order.  Candidates are supplied by the caller from trusted,
room-scoped records; this module never decides authorization.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MentionResult:
    refs: tuple[str, ...]
    ambiguous: tuple[str, ...]


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def find_mentions(text: str, candidates: Mapping[str, str | tuple[str, ...]]) -> MentionResult:
    """``candidates`` maps a ref (Agent id or ``owner``) to its display name (or several accepted names)."""
    by_name: dict[str, set[str]] = {}
    for ref, names in candidates.items():
        for name in (names,) if isinstance(names, str) else names:
            name = (name or "").strip()
            if name:
                by_name.setdefault(name.casefold(), set()).add(ref)
    if not by_name or "@" not in text:
        return MentionResult((), ())
    ordered = sorted(by_name, key=len, reverse=True)
    folded = text.casefold() if len(text.casefold()) == len(text) else None
    refs: list[str] = []
    ambiguous: list[str] = []
    pos = text.find("@")
    while pos != -1:
        nxt = pos + 1
        if pos == 0 or not _word(text[pos - 1]):
            for name in ordered:
                end = pos + 1 + len(name)
                if end > len(text):
                    continue
                piece = folded[pos + 1:end] if folded is not None else text[pos + 1:end].casefold()
                if piece != name:
                    continue
                if end < len(text) and (_word(text[end]) or text[end] == "-"):
                    continue
                owners = by_name[name]
                if len(owners) > 1:
                    if name not in ambiguous:
                        ambiguous.append(name)
                else:
                    ref = next(iter(owners))
                    if ref not in refs:
                        refs.append(ref)
                nxt = end
                break
        pos = text.find("@", nxt)
    return MentionResult(tuple(refs), tuple(ambiguous))
