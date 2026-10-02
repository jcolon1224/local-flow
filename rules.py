"""Free, offline cleanup rules — the local stand-in for the Claude pass.

Handles the common things people do when dictating:
  fillers ("um", "uh"), self-corrections ("Tuesday, no wait, Wednesday"),
  "scratch that", spoken formatting ("new line", "new paragraph",
  "bullet point"), and ordinal lists ("first..., second..., third...").

Every rule is conservative: when unsure, leave the text alone.
"""
import re

FILLERS = r"\b(?:u+m+|u+h+m*|e+r+m+|h+m+|mhm|uh-huh)\b"

# Phrases that mean "replace what I just said with what comes next".
CORRECTION_CUES = [
    "no wait", "wait no", "no no", "no sorry", "sorry no", "actually no",
    "no actually", "sorry i mean", "sorry i meant", "i meant to say",
    "correction", "or rather",
]
# Phrases that mean "throw away the sentence I just said".
DELETE_CUES = ["scratch that", "delete that", "strike that", "never mind that",
               "cancel that", "forget that"]

ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth",
            "seventh", "eighth", "ninth", "tenth"]

_WORD = re.compile(r"[\w'’$%@.:/-]+")


def _cue_pattern(cues: list[str]) -> re.Pattern:
    body = "|".join(r"\W+".join(map(re.escape, c.split())) for c in cues)
    # Allow punctuation around the cue, which Whisper usually inserts.
    return re.compile(r"[\s,;.!?—–-]*\b(?:" + body + r")\b[\s,;.!?—–-]*", re.I)


_CORRECTION = _cue_pattern(CORRECTION_CUES)
_DELETE = _cue_pattern(DELETE_CUES)


def remove_fillers(text: str) -> str:
    text = re.sub(r"[\s,]*" + FILLERS + r"[\s,.]*", " ", text, flags=re.I)
    # "the the" style stutters (not "had had", "that that" is left alone too).
    text = re.sub(r"\b(?!(?:had|that)\b)(\w+)(\s+\1\b)+", r"\1", text, flags=re.I)
    return text


def _apply_one_correction(before: str, after: str) -> str:
    """Merge `before` + correction `after` into the corrected text."""
    after = after.strip()
    if not after:
        return before
    words_after = _WORD.findall(after)
    if not words_after:
        return before + " " + after
    first = words_after[0].lower()
    # Case 1: the speaker restarts the phrase ("send it to Mike, no wait,
    # send it to Sarah") -> cut back to where that phrase began.
    tokens = list(_WORD.finditer(before))
    for m in reversed(tokens[-10:]):
        if m.group(0).lower() == first:
            return before[: m.start()].rstrip() + " " + after
    # Case 2: a single-item swap ("Tuesday, no wait, Wednesday at 3")
    # -> replace the last word before the cue.
    if tokens:
        last = tokens[-1]
        return before[: last.start()].rstrip() + " " + after
    return after


def apply_corrections(text: str) -> str:
    for _ in range(5):  # handle a few corrections in one dictation
        m = _CORRECTION.search(text)
        if not m or m.start() == 0:
            break
        before, after = text[: m.start()], text[m.end():]
        # Only the current sentence is being corrected; keep the rest.
        head, sentence = _split_last_sentence(before)
        text = head + _apply_one_correction(sentence, after)
    return text


def apply_deletions(text: str) -> str:
    for _ in range(5):
        m = _DELETE.search(text)
        if not m:
            break
        head, _ = _split_last_sentence(text[: m.start()])
        text = head + text[m.end():].lstrip()
    return text


def _split_last_sentence(text: str) -> tuple[str, str]:
    idx = max(text.rfind(p) for p in (". ", "? ", "! ", "\n"))
    if idx == -1:
        return "", text
    return text[: idx + 1] + " ", text[idx + 1:].lstrip()


def spoken_formatting(text: str) -> str:
    text = re.sub(r"[\s,.]*\bnew paragraph\b[\s,.]*", "\n\n", text, flags=re.I)
    text = re.sub(r"[\s,.]*\b(?:new|next) line\b[\s,.]*", "\n", text, flags=re.I)
    text = re.sub(r"[\s,.]*\bbullet(?: point)?\b[\s,:.]*", "\n- ", text, flags=re.I)
    return text


def ordinal_lists(text: str) -> str:
    """'Three things. First X, second Y, third Z.' -> intro + numbered list."""
    marks = []
    pos = 0
    for n, word in enumerate(ORDINALS, 1):
        pattern = re.compile(r"(?:^|[\s,.;:])(" + word + r"(?:ly)?|number " + str(n) + r")\b[,:]?\s*", re.I)
        m = pattern.search(text, pos)
        if not m:
            break
        marks.append(m)
        pos = m.end()
    if len(marks) < 3:  # "first" alone is usually just a word, not a list
        return text
    intro = text[: marks[0].start(1)].strip().rstrip(",;")
    items = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start(1) if i + 1 < len(marks) else len(text)
        item = text[m.end(): end].strip()
        item = re.sub(r"[\s,;]*\b(?:and|then|and then)\s*$", "", item, flags=re.I).strip(" ,;")
        item = re.sub(r"^(?:and|then)\s+", "", item, flags=re.I)
        items.append(f"{i + 1}. {_cap(item)}")
    if intro and not intro.endswith((":", ".", "?", "!")):
        intro += ":"
    return (intro + "\n" if intro else "") + "\n".join(items)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def tidy(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])(?=[^\s\d/])", r"\1 ", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = re.sub(r"([.!?])[.,]+", r"\1", text)
    text = re.sub(r"^[\s,.;:]+", "", text)
    # Capitalize sentence starts and line starts.
    text = re.sub(r"(^|[.!?]\s+|\n(?:- )?)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r"\bi\b", "I", text)
    return text.strip()


def clean(text: str) -> str:
    text = remove_fillers(text)
    text = apply_deletions(text)
    text = apply_corrections(text)
    text = spoken_formatting(text)
    text = ordinal_lists(text)
    return tidy(text)
