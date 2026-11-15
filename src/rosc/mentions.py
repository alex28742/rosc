"""P052: canonical ``@DisplayName`` addressing shared by Owner text and authenticated Agent replies.

One deterministic rule.  This module is the AUTHORITATIVE parser: every server route re-parses the text and decides.
The frontend (``frontend/src/presentations/default/ui/mentions.ts``) implements the same rule only as a preview; where
its case folding cannot reproduce Python's (see "Unicode contract" below) the server's decision prevails and an unsafe
route is refused before anything is stored -- the UI never silently picks a different provider route.

Rule (P052 r02):

* text and candidate names are NFC-normalized before matching;
* literal fragments are not addresses: a backtick code span (a run of N backticks up to the next run of the same N
  backticks) is ignored, and so is an ``@`` immediately preceded by a letter, digit, combining mark, ``_``, ``\\``,
  ``/``, ``.`` or another ``@`` (so ``a@Claude.dev`` is an e-mail, ``\\@Claude`` is escaped, ``npm/@types`` a path);
* the display name is compared case-insensitively with Python's full Unicode case folding (``str.casefold``), exactly
  (never a prefix or substring).  The comparison is made against ORIGINAL text characters: each text character is
  folded on its own and the span ends on a real character boundary, so a fold that expands (``ß`` -> ``ss``, ``İ`` ->
  ``i̇``) never shifts or splits the matched span (``@STRASSE`` and ``@Straße`` both address ``Straße``);
* the character after the name must not be a letter, digit, combining mark, ``_`` or ``-`` (so ``@Claude-2`` is not
  ``@Claude``);
* the longest matching span wins at one position, so ``@Claude Opus`` never also counts as ``@Claude``;
* a folded name shared by two different candidates is AMBIGUOUS and resolves to nobody;
* a mention-capable ``@`` directly followed by a letter that matches NO candidate is an UNKNOWN explicit recipient
  (reported in ``unknown``; Owner routes refuse it, an Agent reply simply addresses nobody by it).

Unicode contract: folding is per code point, context free (Python ``casefold`` has no contextual rules), so the folded
span is exactly ``span.casefold()``.  ``İ`` folds to ``i̇`` (dotted), so ``@İvan`` does not address ``Ivan``.

The result is a list of candidate refs in first-mention order.  Candidates are supplied by the caller from trusted,
room-scoped records; this module never decides authorization.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

_CODE_SPAN = re.compile(r"(`+)[\s\S]*?\1")
_LITERAL_BEFORE = "_\\/.@"
_MAX_UNKNOWN = 64


@dataclass(frozen=True)
class MentionResult:
    refs: tuple[str, ...]
    ambiguous: tuple[str, ...]
    unknown: tuple[str, ...] = ()


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _match_end(text: str, start: int, folded_name: str) -> int | None:
    """End index (exclusive) in ``text`` where ``text[start:end].casefold() == folded_name``, or None."""
    acc = ""
    for j in range(start, len(text)):
        acc += text[j].casefold()
        if acc == folded_name:
            return j + 1
        if not folded_name.startswith(acc):
            return None
    return None


def find_mentions(text: str, candidates: Mapping[str, str | tuple[str, ...]]) -> MentionResult:
    """``candidates`` maps a ref (Agent id or ``owner``) to its display name (or several accepted names)."""
    by_name: dict[str, set[str]] = {}
    for ref, names in candidates.items():
        for name in (names,) if isinstance(names, str) else names:
            name = (name or "").strip()
            if name:
                by_name.setdefault(_fold(name), set()).add(ref)
    if not isinstance(text, str) or "@" not in text:
        return MentionResult((), ())
    text = _CODE_SPAN.sub(" ", unicodedata.normalize("NFC", text))
    refs: list[str] = []
    ambiguous: list[str] = []
    unknown: list[str] = []
    pos = text.find("@")
    while pos != -1:
        nxt = pos + 1
        if pos == 0 or not (_word(text[pos - 1]) or text[pos - 1] in _LITERAL_BEFORE):
            best: tuple[int, str] | None = None
            for name in by_name:
                end = _match_end(text, pos + 1, name)
                if end is None or (end < len(text) and (_word(text[end]) or text[end] == "-")):
                    continue
                if best is None or end > best[0]:
                    best = (end, name)
            if best is not None:
                end, name = best
                owners = by_name[name]
                if len(owners) > 1:
                    if name not in ambiguous:
                        ambiguous.append(name)
                else:
                    ref = next(iter(owners))
                    if ref not in refs:
                        refs.append(ref)
                nxt = end
            elif pos + 1 < len(text) and text[pos + 1].isalpha():
                end = pos + 1
                while end < len(text) and (_word(text[end]) or text[end] == "-") and end - pos <= _MAX_UNKNOWN:
                    end += 1
                token = text[pos + 1:end]
                if token not in unknown:
                    unknown.append(token)
                nxt = end
        pos = text.find("@", nxt)
    return MentionResult(tuple(refs), tuple(ambiguous), tuple(unknown))
