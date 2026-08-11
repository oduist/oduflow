"""Unit tests for the pure .po reader in oduflow.po_tools (no Docker needed)."""

from oduflow.po_tools import compare, parse_po, summarize

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
        assert summary.untranslated_msgids == [
            "Only a dispatch manager may accept deals."
        ]

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
        assert diff["missing"] == [
            "Only a dispatch manager may accept deals.",
            "Publish",
        ]
        assert diff["stale"] == ["The Trans.eu service is unreachable: %s"]

    def test_identical_files_have_no_diff(self):
        entries = parse_po(WELL_FORMED)
        assert compare(entries, entries) == {"missing": [], "stale": []}
