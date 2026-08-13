"""Unit tests for the pure .po reader in oduflow.po_tools (no Docker needed)."""

from oduflow import po_tools
from oduflow.po_tools import (
    PoSummary,
    compare,
    diagnose,
    merge_with_template,
    parse_po,
    summarize,
)

HEADER = """msgid ""
msgstr ""
"Project-Id-Version: Odoo Server 15.0\\n"
"Report-Msgid-Bugs-To: \\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: pl\\n"

"""

WELL_FORMED = (
    HEADER
    + """#. module: transeu_bridge
#: model:ir.model.fields,field_description:transeu_bridge.field_transeu_freight__max_price
msgid "Budget Ceiling"
msgstr "Limit budżetu"

#. module: transeu_bridge
#: model_terms:ir.ui.view,arch_db:transeu_bridge.view_freight_form
msgid "Publish"
msgstr "Opublikuj"

#. module: transeu_bridge
#: code:addons/transeu_bridge/models/freight.py:0
#, python-format
msgid "Only a dispatch manager may accept deals."
msgstr ""
"""
)


class TestParsePo:
    def test_header_entry_is_dropped(self):
        assert parse_po(HEADER) == []

    def test_reads_msgid_msgstr_module_and_reference(self):
        entries = parse_po(WELL_FORMED)
        assert [e.msgid for e in entries] == [
            "Budget Ceiling",
            "Publish",
            "Only a dispatch manager may accept deals.",
        ]
        first = entries[0]
        assert first.msgstr == "Limit budżetu"
        assert first.module == "transeu_bridge"
        assert first.occurrences == (
            (
                "model:ir.model.fields,field_description:"
                "transeu_bridge.field_transeu_freight__max_price"
            ),
        )

    def test_reference_types(self):
        assert [e.ref_types for e in parse_po(WELL_FORMED)] == [
            {"model"},
            {"model_terms"},
            {"code"},
        ]

    def test_a_term_referenced_twice_carries_both_types(self):
        # A field label written into a view arch is stored twice by Odoo, so it
        # must show up under both kinds rather than only the first one seen.
        text = (
            HEADER
            + """#. module: mymod
#: model:ir.model.fields,field_description:mymod.field_mymod_deal__name
#: model_terms:ir.ui.view,arch_db:mymod.view_deal_form
msgid "Reference"
msgstr ""
"""
        )
        (entry,) = parse_po(text)
        assert entry.ref_types == {"model", "model_terms"}
        assert summarize([entry]).by_type == {"model": 1, "model_terms": 1}

    def test_interior_whitespace_is_preserved(self):
        # Odoo stores view text verbatim; a term whose newlines/indentation were
        # collapsed no longer matches the source and is silently never applied.
        text = (
            HEADER
            + """#. module: transeu_bridge
#: model_terms:ir.actions.act_window,help:transeu_bridge.action_freight
msgid ""
"Fill in the route and the budget\\n"
"                   ceiling, then publish."
msgstr ""
"""
        )
        (entry,) = parse_po(text)
        assert entry.msgid == (
            "Fill in the route and the budget\n"
            "                   ceiling, then publish."
        )

    def test_obsolete_entries_are_ignored(self):
        text = (
            HEADER
            + """#. module: transeu_bridge
#: code:addons/transeu_bridge/models/freight.py:0
msgid "Kept"
msgstr "Zachowane"

#~ msgid "Dropped"
#~ msgstr "Usunięte"
"""
        )
        assert [e.msgid for e in parse_po(text)] == ["Kept"]

    def test_escapes_are_decoded(self):
        text = (
            HEADER
            + """#. module: transeu_bridge
#: code:addons/transeu_bridge/models/freight.py:0
msgid "Publication failed:\\n%s"
msgstr "Publikacja nie powiodła się:\\n%s"
"""
        )
        (entry,) = parse_po(text)
        assert entry.msgid == "Publication failed:\n%s"


class TestSummarize:
    def test_counts_by_type_sum_to_entries(self):
        summary = summarize(parse_po(WELL_FORMED))
        assert summary.entries == 3
        assert summary.by_type == {"model": 1, "model_terms": 1, "code": 1}
        assert summary.translated == 2
        assert [e.msgid for e in summary.untranslated_terms] == [
            "Only a dispatch manager may accept deals."
        ]

    def test_untranslated_terms_keep_their_reference(self):
        # The reference is what a caller needs to find the term; the msgid of a
        # view term can be a whole page of markup.
        (term,) = summarize(parse_po(WELL_FORMED)).untranslated_terms
        assert term.occurrences == ("code:addons/transeu_bridge/models/freight.py:0",)

    def test_entries_without_reference_are_flagged(self):
        # A valid gettext file that Odoo imports as *zero* translations: with no
        # "#:" line the reader has no target to write to and yields nothing.
        text = (
            HEADER
            + """#. module: transeu_bridge
msgid "Budget Ceiling"
msgstr "Limit budżetu"

#. module: transeu_bridge
msgid "Publish"
msgstr "Opublikuj"
"""
        )
        summary = summarize(parse_po(text))
        assert summary.entries == 2
        assert summary.no_reference == 2
        assert summary.by_type == {}

    def test_entries_without_module_comment_are_flagged(self):
        # These crash Odoo's PoFileReader outright (.groups() on a None match).
        text = (
            HEADER
            + """#: model:ir.model.fields,field_description:transeu_bridge.field_a__b
msgid "Budget Ceiling"
msgstr "Limit budżetu"
"""
        )
        summary = summarize(parse_po(text))
        assert summary.no_module_comment == 1
        assert summary.no_reference == 0

    def test_unknown_reference_kind_counts_as_other(self):
        text = (
            HEADER
            + """#. module: transeu_bridge
#: selection:transeu.freight,state:0
msgid "Negotiating"
msgstr "Negocjacje"
"""
        )
        assert summarize(parse_po(text)).by_type == {"other": 1}


class TestCompare:
    def test_missing_and_stale(self):
        template = parse_po(WELL_FORMED)
        translation = parse_po(
            HEADER
            + """#. module: transeu_bridge
#: model:ir.model.fields,field_description:transeu_bridge.field_a__b
msgid "Budget Ceiling"
msgstr "Limit budżetu"

#. module: transeu_bridge
#: code:addons/transeu_bridge/models/freight.py:0
msgid "The Trans.eu service is unreachable: %s"
msgstr "Usługa Trans.eu jest niedostępna: %s"
"""
        )
        diff = compare(template, translation)
        assert [e.msgid for e in diff["missing"]] == [
            "Only a dispatch manager may accept deals.",
            "Publish",
        ]
        assert [e.msgid for e in diff["stale"]] == [
            "The Trans.eu service is unreachable: %s"
        ]

    def test_diff_entries_carry_the_reference_that_locates_them(self):
        template = parse_po(WELL_FORMED)
        (publish,) = [
            e for e in compare(template, [])["missing"] if e.msgid == "Publish"
        ]
        assert publish.occurrences == (
            "model_terms:ir.ui.view,arch_db:transeu_bridge.view_freight_form",
        )

    def test_identical_files_have_no_diff(self):
        entries = parse_po(WELL_FORMED)
        assert compare(entries, entries) == {"missing": [], "stale": []}


class TestMergeWithTemplate:
    def test_template_supplies_import_metadata_without_losing_translation(self):
        translation = parse_po(
            HEADER + 'msgid "Budget Ceiling"\nmsgstr "Limit budżetu"\n'
        )
        template = parse_po(WELL_FORMED)

        merged = merge_with_template(translation, template)

        assert [entry.msgid for entry in merged] == [
            "Budget Ceiling",
            "Publish",
            "Only a dispatch manager may accept deals.",
        ]
        assert merged[0].msgstr == "Limit budżetu"
        assert merged[0].module == "transeu_bridge"
        assert merged[0].ref_types == {"model"}
        assert merged[1].msgstr == ""

    def test_entries_absent_from_template_are_obsolete_for_import(self):
        translation = parse_po(
            HEADER
            + """#. module: transeu_bridge
#: code:addons/transeu_bridge/models/old.py:0
msgid "Old string"
msgstr "Stary tekst"
"""
        )
        merged = merge_with_template(translation, parse_po(WELL_FORMED))

        assert merged
        assert "Old string" not in {entry.msgid for entry in merged}


def _summary(entries: int, translated: int, **overrides) -> PoSummary:
    """A PoSummary with only the numbers a verdict is made of."""
    base = dict(
        entries=entries,
        translated=translated,
        untranslated=entries - translated,
        by_type={},
        no_reference=0,
        no_module_comment=0,
        untranslated_terms=[],
    )
    base.update(overrides)
    return PoSummary(**base)  # type: ignore[arg-type]


class TestDiagnose:
    """The verdict is the answer callers came for; these are its edges."""

    TEMPLATE = _summary(442, 442)

    def test_an_inactive_language_hides_every_other_problem(self):
        # Nothing can load, so a broken file is not the thing to report.
        status = diagnose(
            self.TEMPLATE,
            active=False,
            file=_summary(435, 435, no_reference=435),
            effective=_summary(435, 435, no_reference=435),
        )
        assert status.code == po_tools.NOT_ACTIVATED

    def test_no_file_at_all(self):
        status = diagnose(self.TEMPLATE, active=True, database=_summary(442, 0))
        assert status.code == po_tools.NO_FILE

    def test_a_file_odoo_reads_and_discards(self):
        # 311 valid-looking entries, none with a "#:" line: the silent case.
        broken = _summary(311, 311, no_reference=311)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 0),
            file=broken,
            effective=broken,
            missing=131,
        )
        assert status.code == po_tools.IMPORT_DROPPED

    def test_a_missing_module_comment_outranks_a_missing_reference(self):
        # Both are present, but this one aborts the import outright.
        broken = _summary(311, 311, no_reference=311, no_module_comment=311)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 0),
            file=broken,
            effective=broken,
        )
        assert status.code == po_tools.IMPORT_ABORTS

    def test_a_handful_of_bad_entries_is_not_the_headline(self):
        # Four bad entries in an otherwise loaded file: still a warning, but the
        # verdict describes the file as a whole.
        file = _summary(442, 442, no_reference=4)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 438),
            file=file,
            effective=file,
        )
        assert status.code == po_tools.OK

    def test_a_file_covering_almost_nothing_reads_as_untranslated(self):
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 20),
            file=_summary(3, 3),
            effective=_summary(3, 3),
            missing=439,
        )
        assert status.code == po_tools.NOT_TRANSLATED
        assert status.covered_terms == 3
        assert status.coverage == 20 / 442

    def test_a_good_file_that_never_reached_the_database(self):
        file = _summary(435, 435)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 0),
            file=file,
            effective=file,
            missing=7,
        )
        assert status.code == po_tools.NOT_LOADED

    def test_loaded_but_incomplete_counts_what_did_not_apply(self):
        file = _summary(435, 435)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 379),
            file=file,
            effective=file,
            missing=7,
        )
        assert status.code == po_tools.PARTIAL
        assert status.not_applied == 56
        assert status.covered_terms == 435

    def test_complete_translation_is_ok(self):
        file = _summary(442, 442)
        status = diagnose(
            self.TEMPLATE,
            active=True,
            database=_summary(442, 442),
            file=file,
            effective=file,
        )
        assert status.code == po_tools.OK
        assert status.not_applied == 0

    def test_a_module_with_no_terms_does_not_divide_by_zero(self):
        empty = _summary(0, 0)
        status = diagnose(
            empty, active=True, database=empty, file=empty, effective=empty
        )
        assert status.coverage == 0.0
