"""Reading and summarising gettext ``.po``/``.pot`` files the way Odoo does.

Odoo's translation importer is unusually silent about malformed input, and the
two failure modes it hides are the expensive ones:

* After Odoo's optional sibling-POT merge, an entry with no ``#:`` reference
  line carries no type/target, so
  ``PoFileReader.__iter__`` (``odoo/tools/translate.py``) simply yields nothing
  for it. A whole, perfectly valid gettext file can therefore import to *zero*
  translations while the log only says "loading …".
* An entry still lacking ``#. module: <name>`` makes that same reader call
  ``.groups()`` on a ``None`` match, i.e. it aborts the import outright.

So the counts here are deliberately built around what Odoo's *reader* would make
of a file, not around what gettext considers well-formed. Everything in this
module is pure — no Docker, no database — so the interesting cases are covered
by ordinary unit tests.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import partial

# The reference kinds Odoo derives a translation's type from. ``model_terms``
# must be tested before ``model`` — it is a longer prefix of the same shape.
_TYPE_PREFIXES = (
    ("model_terms:", "model_terms"),
    ("model:", "model"),
    ("code:", "code"),
)

# Display order for reports; "other" collects references Odoo would log as
# "unknown occurrence" (e.g. the deprecated ``selection:``/``sql_constraint:``).
TYPE_ORDER = ("model", "model_terms", "code", "other")

# Matches the first line of the ``#.`` comment block, which is where Odoo's
# reader anchors its own ``re.match`` for the owning module.
_MODULE_COMMENT_RE = re.compile(r"^modules?: (\w+)")

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


@dataclass(frozen=True)
class PoEntry:
    """One translatable term as Odoo's reader would see it."""

    msgid: str
    msgstr: str
    #: Owning module from ``#. module: <name>``; empty when the comment is absent.
    module: str
    #: Verbatim ``#:`` reference targets, in file order.
    occurrences: tuple[str, ...] = ()
    #: 1-based line of the entry's ``msgid``, for pointing at a bad entry.
    line: int = 0

    @property
    def ref_types(self) -> set[str]:
        """Translation types Odoo would assign this term, one per reference kind.

        A term can legitimately appear in several places — a label that is also
        written into a view arch is both ``model`` and ``model_terms``, and Odoo
        stores a translation for each. Empty when the entry has no ``#:`` line at
        all, which is the silent-drop case.
        """
        types = set()
        for occurrence in self.occurrences:
            for prefix, name in _TYPE_PREFIXES:
                if occurrence.startswith(prefix):
                    types.add(name)
                    break
            else:
                types.add("other")
        return types


def _unquote(literal: str) -> str:
    """Decode one ``"..."`` po string literal.

    Interior whitespace is preserved exactly: Odoo stores view and help text
    verbatim, newlines and indentation included, so a term whose whitespace has
    been normalised no longer matches the source and is never applied.
    """
    inner = literal[1:-1]
    out: list[str] = []
    escaped = False
    for char in inner:
        if escaped:
            out.append(_ESCAPES.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


@dataclass
class _Pending:
    """Accumulator for the entry currently being read."""

    comments: list[str] = field(default_factory=list)
    occurrences: list[str] = field(default_factory=list)
    msgid: list[str] = field(default_factory=list)
    msgstr: list[str] = field(default_factory=list)
    line: int = 0
    target: str = ""  # "msgid" | "msgstr" | "" while still in the comment block

    def finish(self) -> PoEntry | None:
        if not self.target:
            return None
        module = ""
        if self.comments:
            match = _MODULE_COMMENT_RE.match(self.comments[0])
            if match:
                module = match.group(1)
        return PoEntry(
            msgid="".join(self.msgid),
            msgstr="".join(self.msgstr),
            module=module,
            occurrences=tuple(self.occurrences),
            line=self.line,
        )


def parse_po(text: str) -> list[PoEntry]:
    """Parse ``.po``/``.pot`` text into entries.

    The header entry (``msgid ""``) and obsolete entries (``#~``) are dropped,
    matching what the importer would consider.
    """
    entries: list[PoEntry] = []
    pending = _Pending()

    def flush() -> None:
        nonlocal pending
        entry = pending.finish()
        if entry is not None and entry.msgid:
            entries.append(entry)
        pending = _Pending()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#~"):
            # A blank line closes the current entry; obsolete entries are skipped
            # wholesale, since Odoo ignores them too.
            if not line:
                flush()
            continue
        if line.startswith("#"):
            # A comment after a finished entry starts the next one.
            if pending.target:
                flush()
            if line.startswith("#."):
                pending.comments.append(line[2:].strip())
            elif line.startswith("#:"):
                pending.occurrences.extend(line[2:].split())
            continue
        if line.startswith("msgid "):
            if pending.target:
                flush()
            pending.target = "msgid"
            pending.line = lineno
            pending.msgid.append(_unquote(line[len("msgid ") :].strip()))
        elif line.startswith("msgstr "):
            pending.target = "msgstr"
            pending.msgstr.append(_unquote(line[len("msgstr ") :].strip()))
        elif line.startswith('"') and pending.target:
            # Continuation of the multi-line literal opened above.
            target = pending.msgid if pending.target == "msgid" else pending.msgstr
            target.append(_unquote(line))
        elif line.startswith("msgctxt "):
            # Odoo never writes contexts; tolerate them rather than misparse.
            if pending.target:
                flush()

    flush()
    return entries


@dataclass(frozen=True)
class PoSummary:
    """What a ``.po``/``.pot`` file contains, and how it would import."""

    entries: int
    translated: int
    untranslated: int
    #: Terms carrying each Odoo translation type, in ``TYPE_ORDER``. A term
    #: referenced in more than one place counts under each, so this does not
    #: partition ``entries``.
    by_type: dict[str, int]
    #: Entries with no ``#:`` line — imported as nothing, without a warning.
    no_reference: int
    #: Entries with no ``#. module:`` comment — these abort the import.
    no_module_comment: int
    #: Entries with an empty msgstr, in file order. Whole entries rather than
    #: bare msgids: a term is identified by *where* it lives, and a report's
    #: msgid can be a page of markup.
    untranslated_terms: list[PoEntry]


def summarize(entries: list[PoEntry]) -> PoSummary:
    """Counts and defect flags for a parsed file."""
    by_type: Counter[str] = Counter()
    no_reference = 0
    no_module = 0
    untranslated: list[PoEntry] = []

    for entry in entries:
        ref_types = entry.ref_types
        if not ref_types:
            no_reference += 1
        by_type.update(ref_types)
        if not entry.module:
            no_module += 1
        if not entry.msgstr:
            untranslated.append(entry)

    return PoSummary(
        entries=len(entries),
        translated=len(entries) - len(untranslated),
        untranslated=len(untranslated),
        by_type={name: by_type[name] for name in TYPE_ORDER if by_type[name]},
        no_reference=no_reference,
        no_module_comment=no_module,
        untranslated_terms=untranslated,
    )


def merge_with_template(
    translation: list[PoEntry], template: list[PoEntry]
) -> list[PoEntry]:
    """Return the catalogue Odoo effectively imports beside a sibling POT.

    Odoo asks polib to merge ``<module>.pot`` into a language catalogue before
    iterating it.  Matching entries retain their translated ``msgstr`` but get
    the template's current module comment and occurrences; entries removed from
    the template become obsolete, while new template entries are added with an
    empty translation.  Reproducing those import-relevant effects keeps status
    warnings honest without making polib a runtime dependency.

    The raw translation must still be passed separately to :func:`compare`, so
    missing and stale terms describe the committed file rather than this merged
    view.
    """
    translated_by_id = {entry.msgid: entry for entry in translation}
    merged: list[PoEntry] = []
    for template_entry in template:
        translated = translated_by_id.get(template_entry.msgid)
        merged.append(
            PoEntry(
                msgid=template_entry.msgid,
                msgstr=translated.msgstr if translated else "",
                module=template_entry.module,
                occurrences=template_entry.occurrences,
                line=translated.line if translated else template_entry.line,
            )
        )
    return merged


def _absent_from(entries: list[PoEntry], other: set[str]) -> list[PoEntry]:
    """Entries whose msgid is not in *other*, one per msgid, sorted by msgid."""
    seen: set[str] = set()
    picked: list[PoEntry] = []
    for entry in entries:
        if entry.msgid in other or entry.msgid in seen:
            continue
        seen.add(entry.msgid)
        picked.append(entry)
    return sorted(picked, key=lambda entry: entry.msgid)


def compare(
    template: list[PoEntry], translation: list[PoEntry]
) -> dict[str, list[PoEntry]]:
    """Diff a ``.pot`` against a ``.po`` by msgid.

    ``missing`` are terms the module exposes but the file never mentions — new
    strings someone forgot. ``stale`` are the reverse: entries left over after
    the source string changed or stopped being translatable, which is how dead
    translations survive unnoticed.

    Whole entries come back rather than msgids, because the useful handle on a
    term is its ``#:`` reference: it names the field or view to fix and stays
    short, while the msgid of a report term can be a screenful of markup.
    """
    template_ids = {entry.msgid for entry in template}
    translation_ids = {entry.msgid for entry in translation}
    return {
        "missing": _absent_from(template, translation_ids),
        "stale": _absent_from(translation, template_ids),
    }


# -- Verdicts -----------------------------------------------------------------
#
# The three catalogues are only raw material; what a caller wants to know is
# whether the translation landed and what to do next. That judgement is made
# here, from the numbers alone, so every caller reads the same conclusion.

#: The language is not activated, so nothing can load into the database at all.
NOT_ACTIVATED = "not_activated"
#: The module ships no catalogue for this language.
NO_FILE = "no_file"
#: Entries lack ``#. module:`` — Odoo's reader raises instead of importing.
IMPORT_ABORTS = "import_aborts"
#: Entries lack ``#:`` — Odoo reads them and stores nothing, without a warning.
IMPORT_DROPPED = "import_dropped"
#: A catalogue exists but barely covers the module: nobody translated this yet.
NOT_TRANSLATED = "not_translated"
#: The file is fine and the database is empty — it was never loaded.
NOT_LOADED = "not_loaded"
#: Loaded, with terms still missing or not applied.
PARTIAL = "partial"
#: File, database and module agree.
OK = "ok"

# A defect this widespread is the headline; a handful of bad entries in an
# otherwise good file is reported as a warning beside the real verdict.
_DEFECT_SHARE = 0.5
# Below this share of the module's terms, listing what is missing is the same
# statement as "this language was never translated", only much longer.
_COVERED_SHARE = 0.1
# What did reach the database, as a share of what the file offers.
_LOADED_SHARE = 0.1
# Terms may legitimately resist translation (identifiers, symbols), so the
# database is never expected to match the module term for term.
_COMPLETE_SHARE = 0.95


@dataclass(frozen=True)
class LangStatus:
    """What became of one language, and the numbers behind that conclusion."""

    code: str
    #: Translatable terms the module exposes — the denominator for everything.
    template_terms: int
    #: Module terms the committed file actually carries.
    covered_terms: int
    #: Module terms the database holds a translation for.
    db_terms: int
    #: Translated file entries with no counterpart in the database.
    not_applied: int

    @property
    def coverage(self) -> float:
        """Share of the module's terms translated in the database."""
        if not self.template_terms:
            return 0.0
        return self.db_terms / self.template_terms


def diagnose(
    template: PoSummary,
    *,
    active: bool,
    database: PoSummary | None = None,
    file: PoSummary | None = None,
    effective: PoSummary | None = None,
    missing: int = 0,
) -> LangStatus:
    """Judge one language from the three catalogues.

    *effective* is the file as Odoo would import it (after the sibling-POT
    merge, when there is one); *missing* is how many of the module's terms the
    file never mentions. The order of the checks is the order in which the
    failures mask each other: an inactive language hides a broken file, and a
    broken file hides an empty translation.
    """
    db_terms = database.translated if database else 0
    in_file = file.translated if file else 0
    # With no file there is no diff to subtract, and "all terms covered" is the
    # one thing that must not be reported for a catalogue that does not exist.
    covered = max(template.entries - missing, 0) if file else 0
    status = partial(
        LangStatus,
        template_terms=template.entries,
        covered_terms=covered,
        db_terms=db_terms,
        not_applied=max(in_file - db_terms, 0),
    )

    if not active:
        return status(NOT_ACTIVATED)
    if file is None or effective is None:
        return status(NO_FILE)
    if effective.no_module_comment >= max(effective.entries * _DEFECT_SHARE, 1):
        return status(IMPORT_ABORTS)
    if effective.no_reference >= max(effective.entries * _DEFECT_SHARE, 1):
        return status(IMPORT_DROPPED)
    if template.entries and covered <= template.entries * _COVERED_SHARE:
        return status(NOT_TRANSLATED)
    if in_file and db_terms <= in_file * _LOADED_SHARE:
        return status(NOT_LOADED)
    if not missing and db_terms >= template.entries * _COMPLETE_SHARE:
        return status(OK)
    return status(PARTIAL)
