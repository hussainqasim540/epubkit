"""
EPUB filename reducer.

Pure logic for shortening verbose EPUB filenames. No I/O side effects here —
the CLI layer (cli.py) handles file walking, plan writing, and rename apply.

The reducer takes a list of EPUB paths and produces a list of (old_path, new_name)
proposals. It auto-detects the common prefix within each parent directory so the
series name (which is usually repeated in every file in a folder) gets stripped.

Examples (with parent folder grouping):
  "Ascendance of a Bookworm_ Part 4 Founder ... Volume 9 - Miya Kazuki.epub"
    -> "p4v09 - Founder of the Royal Academy's So-Called Library Committee.epub"
  "That Time I Got Reincarnated as a Slime - Volume 12 [Yen Press]{v2}[Kobo].epub"
    -> "v12.epub"
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


BRACKET_RE = re.compile(r"\s*[\[\{\(][^\[\]\{\}\(\)]*[\]\}\)]")
VOLUME_RE = re.compile(r"\b(?:Volume|Vol\.?|v)\s*(\d+)\b", re.IGNORECASE)
PART_RE = re.compile(r"\bPart\s*(\d+)\b", re.IGNORECASE)
# Fallback: a bare trailing number (e.g. "Official Fanbook 4") — only used when
# no explicit Volume/Vol/v keyword was found. Captures the number and the space
# before it so the cleanup leaves a tidy subtitle.
TRAILING_NUMBER_RE = re.compile(r"\s+(\d{1,3})\s*$")
# If the auto-detected common prefix ends with a volume keyword (",  Vol. ", " Volume "),
# snap it back so per-file extraction still sees the keyword and produces "v10" not "10".
PREFIX_VOL_TAIL_RE = re.compile(r"[,\s\-_:]*(?:Volume|Vol\.?|v)[\s\.]*$", re.IGNORECASE)
SEPARATOR_CLEANUP_RE = re.compile(r"\s*[-_:,]+\s*$")
LEADING_JUNK_RE = re.compile(r"^[\s\-_:,]+")
MULTI_SPACE_RE = re.compile(r"\s+")

# Acronym rules: drop articles entirely, lowercase letter for common connectives,
# uppercase first letter for everything else. "Ascendance of a Bookworm" -> "AoB".
ACRONYM_SKIP = {"a", "an", "the"}
ACRONYM_LOWER = {"of", "and", "or", "but", "in", "on", "at", "by", "for", "to",
                 "with", "as", "from", "into", "vs"}


@dataclass
class RenameProposal:
    old_path: Path
    new_name: str
    status: str = "ok"  # ok | unchanged | conflict | no-match
    reason: str = ""


@dataclass
class GroupResult:
    directory: Path
    common_prefix: str
    proposals: list[RenameProposal] = field(default_factory=list)
    abbreviation: tuple[str, str] | None = None
    series_acronym: str = ""


def strip_brackets(name: str) -> str:
    """Remove [...], {...}, (...) groups and surrounding whitespace."""
    prev = None
    while prev != name:
        prev = name
        name = BRACKET_RE.sub("", name)
    return name


# Words that, if present in a segment, mean it's not a person's name —
# usually a category/subtitle indicator. Lowercase for case-insensitive match.
_NON_AUTHOR_HINTS = {
    "fanbook", "fanbooks", "collection", "stories", "story", "edition",
    "chapter", "bonus", "special", "volume", "vol", "deluxe", "limited",
    "anniversary", "side", "extra", "guide", "art", "artbook", "compendium",
    "omnibus", "year", "years", "season", "tales", "saga", "novel", "novels",
    "book", "books", "part", "epilogue", "prologue", "interlude", "official",
    "complete", "definitive", "annotated", "illustrated", "diary", "diaries",
}


def _looks_like_author(segment: str) -> bool:
    """
    Strict heuristic: True if a dash-separated segment looks like a person's
    name (e.g. "Miya Kazuki", "You Shiina", "Kazuki, Miya"). Subtitles that
    happen to be 2 Title-Case words (like "Official Fanbook", "First Year")
    are excluded via the _NON_AUTHOR_HINTS keyword list.
    """
    s = segment.strip()
    if not s or len(s) > 30:
        return False
    if any(c.isdigit() for c in s):
        return False
    lower_words = {w.lower().strip("'’.,") for w in s.split()}
    if lower_words & _NON_AUTHOR_HINTS:
        return False
    # "Lastname, Firstname" form
    if "," in s:
        halves = [h.strip() for h in s.split(",")]
        if len(halves) == 2 and all(2 <= len(h) <= 20 for h in halves):
            return True
    words = s.split()
    # Strict: exactly 2 capitalized words (typical Firstname Lastname).
    if len(words) != 2:
        return False
    return all(w[:1].isupper() for w in words)


def make_acronym(phrase: str) -> str:
    """'Ascendance of a Bookworm' -> 'AoB'."""
    out = []
    for word in phrase.split():
        clean = "".join(c for c in word if c.isalnum())
        if not clean:
            continue
        low = clean.lower()
        if low in ACRONYM_SKIP:
            continue
        if low in ACRONYM_LOWER:
            out.append(low[0])
        else:
            out.append(clean[0].upper())
    return "".join(out)


def find_common_substring(stems: list[str], min_chars: int = 10,
                          min_words: int = 3) -> str:
    """
    Longest common substring across all stems that has at least ``min_words``
    words and ``min_chars`` characters. Returns '' if none qualifies. Used to
    detect series titles that aren't a clean prefix/suffix (e.g. when authors
    appear in different positions across files).
    """
    if len(stems) < 2:
        return ""
    shortest = min(stems, key=len)
    n = len(shortest)
    # Decreasing length so the first hit is the longest.
    for length in range(n, min_chars - 1, -1):
        for start in range(n - length + 1):
            # Anchor on non-alphanumeric boundaries so we don't slice mid-word
            if start > 0 and shortest[start - 1].isalnum():
                continue
            end = start + length
            if end < n and shortest[end].isalnum():
                continue
            sub = shortest[start:end]
            if len(sub.split()) < min_words:
                continue
            if all(sub in s for s in stems):
                # Trim non-alphanumeric edges so the caller can do a clean
                # word-level replace without eating surrounding separators.
                return sub.strip(" -_:,;.")
    return ""


def detect_common_suffix(stems: list[str]) -> str:
    """
    Find the longest common suffix across filenames in a group, snapped to a
    natural word boundary. Catches trailing " - Author" patterns where the
    author repeats across all files in a series.
    """
    if len(stems) < 2:
        return ""
    suffix = stems[0]
    for s in stems[1:]:
        i = 0
        limit = min(len(suffix), len(s))
        while i < limit and suffix[-1 - i] == s[-1 - i]:
            i += 1
        suffix = suffix[len(suffix) - i:] if i > 0 else ""
        if not suffix:
            return ""
    # Snap forward to the next natural boundary
    for sep in (" - ", " — ", ": "):
        idx = suffix.find(sep)
        if idx >= 0:
            suffix = suffix[idx:]
            break
    return suffix if len(suffix.strip(" -_:")) >= 3 else ""


def detect_common_prefix(stems: list[str]) -> str:
    """
    Find the longest common prefix across filenames in a group, snapped to a
    natural word boundary (space, dash, underscore, colon). Returns '' if the
    prefix is too short to be meaningful (<6 chars) or if there's only one file.
    """
    if len(stems) < 2:
        return ""
    prefix = stems[0]
    for s in stems[1:]:
        i = 0
        limit = min(len(prefix), len(s))
        while i < limit and prefix[i] == s[i]:
            i += 1
        prefix = prefix[:i]
        if not prefix:
            return ""
    # Snap back to last natural boundary
    for sep in (" - ", "_ ", ": ", " "):
        idx = prefix.rfind(sep)
        if idx > 0:
            prefix = prefix[: idx + len(sep)]
            break
    return prefix if len(prefix.strip()) >= 6 else ""


def reduce_one(
    stem: str,
    common_prefix: str,
    common_suffix: str = "",
    vol_pad: int = 2,
    series_acronym: str = "",
    inline_phrase: str = "",
) -> tuple[str, str]:
    """
    Reduce a single filename stem (no .epub extension). Returns (new_stem, status).
    status is 'ok' if any reduction happened, 'unchanged' otherwise.
    """
    original = stem
    work = stem

    # 1. Strip auto-detected common series prefix and suffix
    if common_prefix and work.startswith(common_prefix):
        work = work[len(common_prefix):]
    if common_suffix and work.endswith(common_suffix):
        work = work[: -len(common_suffix)]

    # 2. Strip bracketed noise
    work = strip_brackets(work)

    # 3. If the series title still appears inline (LCS detection), replace it
    #    with the acronym so we can filter that segment out later.
    if inline_phrase and series_acronym and inline_phrase in work:
        work = work.replace(inline_phrase, series_acronym)

    # 4. Extract Part N and Volume N
    part_match = PART_RE.search(work)
    vol_match = VOLUME_RE.search(work)
    # Fallback: if no Volume/Vol/v keyword, look for a bare trailing number.
    # This catches patterns like "Official Fanbook 4".
    if not vol_match and not part_match:
        vol_match = TRAILING_NUMBER_RE.search(work)
    part_num = int(part_match.group(1)) if part_match else None
    vol_num = int(vol_match.group(1)) if vol_match else None

    # Remove the matched Part/Volume tokens from the remainder.
    # Remove the later match first so the earlier match's offsets stay valid.
    spans = []
    if part_match:
        spans.append(part_match.span())
    if vol_match:
        spans.append(vol_match.span())
    for start, end in sorted(spans, reverse=True):
        work = work[:start] + work[end:]

    # 5. Split remainder into segments. Drop authors and the acronym
    # (which we'll re-insert in a fixed position). What's left is the subtitle.
    work = MULTI_SPACE_RE.sub(" ", work)
    raw_segments = re.split(r"\s+-\s+|_\s+", work)
    other_segments: list[str] = []
    for seg in raw_segments:
        seg = LEADING_JUNK_RE.sub("", seg)
        seg = SEPARATOR_CLEANUP_RE.sub("", seg).strip()
        if not seg:
            continue
        if series_acronym and seg == series_acronym:
            continue
        if _looks_like_author(seg):
            continue
        other_segments.append(seg)

    # 6. Build the new stem. Order requested by user:
    #    volume -> part -> series-acronym -> remaining subtitle
    parts: list[str] = []
    if vol_num is not None:
        parts.append(f"v{vol_num:0{vol_pad}d}")
    if part_num is not None:
        parts.append(f"p{part_num}")
    if series_acronym:
        parts.append(series_acronym)
    parts.extend(other_segments)

    if not parts:
        new_stem = MULTI_SPACE_RE.sub(" ", work).strip(" -_:") or original
    else:
        new_stem = " - ".join(parts)

    new_stem = unicodedata.normalize("NFC", new_stem)
    status = "ok" if new_stem != original else "unchanged"
    return new_stem, status


def reduce_group(files: list[Path]) -> GroupResult:
    """Reduce a list of EPUBs sharing the same parent directory."""
    if not files:
        return GroupResult(directory=Path("."), common_prefix="")

    directory = files[0].parent
    stems = [f.stem for f in files]
    prefix = detect_common_prefix(stems)
    # If the prefix swallowed a volume keyword (e.g. "..., Vol. "), snap it back
    # so per-file reduction can still normalize "10" -> "v10".
    m = PREFIX_VOL_TAIL_RE.search(prefix)
    if m:
        prefix = prefix[: m.start()]
    suffix = detect_common_suffix(stems)

    # Determine the series acronym. We always want it in the output, so try
    # two sources in order:
    #   1. A multi-word LCS that survives prefix/suffix strip (handles cases
    #      where the series sits between author and volume across files).
    #   2. The common prefix itself, when present (the series got stripped
    #      cleanly from the front — we still want its acronym in the result).
    stripped = []
    for s in stems:
        t = s
        if prefix and t.startswith(prefix):
            t = t[len(prefix):]
        if suffix and t.endswith(suffix):
            t = t[: -len(suffix)]
        stripped.append(t)
    common_phrase = find_common_substring(stripped)
    series_acronym = ""
    inline_phrase = ""
    if common_phrase:
        cand = make_acronym(common_phrase)
        if 2 <= len(cand) <= 8:
            series_acronym = cand
            inline_phrase = common_phrase
    if not series_acronym and prefix:
        clean_prefix = prefix.strip(" -_:,;.")
        if len(clean_prefix.split()) >= 2:
            cand = make_acronym(clean_prefix)
            if 2 <= len(cand) <= 8:
                series_acronym = cand

    # Pad volume numbers to width of the largest volume in the group
    max_vol = 0
    for s in stems:
        m = VOLUME_RE.search(s)
        if m:
            max_vol = max(max_vol, int(m.group(1)))
    vol_pad = max(2, len(str(max_vol))) if max_vol else 2

    proposals = []
    seen_names: dict[str, Path] = {}
    for f in files:
        new_stem, status = reduce_one(f.stem, prefix, suffix, vol_pad=vol_pad,
                                      series_acronym=series_acronym,
                                      inline_phrase=inline_phrase)
        new_name = f"{new_stem}.epub"
        if status == "unchanged" and new_name == f.name:
            proposals.append(RenameProposal(old_path=f, new_name=new_name, status="unchanged"))
            continue
        if new_name in seen_names:
            proposals.append(RenameProposal(
                old_path=f, new_name=new_name, status="conflict",
                reason=f"collides with {seen_names[new_name].name}",
            ))
            continue
        seen_names[new_name] = f
        proposals.append(RenameProposal(old_path=f, new_name=new_name, status=status))

    abbreviation = (inline_phrase, series_acronym) if inline_phrase else None
    return GroupResult(directory=directory, common_prefix=prefix,
                       proposals=proposals, abbreviation=abbreviation,
                       series_acronym=series_acronym)


def reduce_paths(paths: list[Path], recursive: bool = False) -> list[GroupResult]:
    """
    Walk paths (files or directories), group .epub files by parent dir,
    and return one GroupResult per directory.
    """
    files: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == ".epub":
            files.append(p)
        elif p.is_dir():
            pattern = "**/*.epub" if recursive else "*.epub"
            files.extend(sorted(p.glob(pattern)))

    # Filter macOS AppleDouble resource-fork sidecars ("._*") — they're not real EPUBs.
    files = [f for f in files if not f.name.startswith("._")]

    groups: dict[Path, list[Path]] = {}
    for f in files:
        groups.setdefault(f.parent, []).append(f)

    return [reduce_group(sorted(group)) for group in groups.values()]
