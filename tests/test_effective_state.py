import unittest

from spec_enricher.effective_state import Operation, blocks, resolve


class EffectiveStateTests(unittest.TestCase):
    def test_quoted_replacement_value_does_not_silently_narrow_the_target(self):
        base = blocks("base", b"The header may be Authorization. Keep ownership checks.\n")
        delta = blocks("delta", b'Replace the header policy with "Authorization" only.\n')
        selected = "The header may be Authorization."
        state = resolve(
            base,
            delta,
            [
                Operation(
                    "r", "replace", delta[0].id, base[0].id, base[0].hash, target_quote=selected
                )
            ],
        )
        self.assertFalse(state.unresolved)
        self.assertTrue(any("Keep ownership checks." in b.text for b in state.blocks))
        self.assertFalse(any(b.text == "The header may be " for b in state.blocks))

    def test_quoted_sentence_change_preserves_neighboring_assertions_and_utf8_anchors(self):
        original = "Доступ ограничен. MCP выключен. Журнал обязателен.\n".encode()
        base = blocks("base", original)
        delta = blocks("delta", "Заменить «MCP выключен.»: протокол пока неизвестен.\n".encode())
        state = resolve(
            base, delta, [Operation("r", "replace", delta[0].id, base[0].id, base[0].hash)]
        )
        self.assertFalse(state.unresolved)
        preserved = [b for b in state.blocks if b.snapshot_id == "base"]
        self.assertEqual(
            [b.text for b in preserved], ["Доступ ограничен. ", " Журнал обязателен.\n"]
        )
        for block in preserved:
            self.assertEqual(original[block.byte_start : block.byte_end], block.text.encode())
        self.assertIn(delta[0], state.blocks)
        self.assertEqual(base[0].text.encode(), original)

    def test_disjoint_spans_apply_together_and_overlaps_never_partially_apply(self):
        base = blocks("base", b"Auth on. Logs on. Ownership on.\n")
        delta = blocks("delta", b"Disable auth.\n\nDisable logs.\n")
        operations = [
            Operation(
                "a", "replace", delta[0].id, base[0].id, base[0].hash, target_quote="Auth on."
            ),
            Operation(
                "b", "delete", delta[1].id, base[0].id, base[0].hash, target_quote="Logs on."
            ),
        ]
        state = resolve(base, delta, operations)
        self.assertFalse(state.unresolved)
        self.assertTrue(any("Ownership on." in b.text for b in state.blocks))
        overlapping = [
            operations[0],
            Operation("b", "delete", delta[1].id, base[0].id, base[0].hash),
        ]
        state = resolve(base, delta, overlapping)
        self.assertEqual(state.blocks, tuple(base))
        self.assertEqual(len(state.unresolved), 2)

    def test_explicit_span_cannot_expand_to_paragraph_or_choose_repeated_text(self):
        base = blocks("base", b"Auth on. Logs on.\n")
        delta = blocks("delta", b'Replace "Auth on." with a changed authorization rule.\n')
        state = resolve(
            base,
            delta,
            [
                Operation(
                    "r", "replace", delta[0].id, base[0].id, base[0].hash, target_quote=base[0].text
                )
            ],
        )
        self.assertEqual(state.blocks, tuple(base))
        self.assertEqual(
            state.unresolved[0]["reason"], "target_quote_conflicts_with_explicit_delta_quote"
        )
        base = blocks("base", b"Auth on. Auth on.\n")
        state = resolve(
            base, delta, [Operation("r", "delete", delta[0].id, base[0].id, base[0].hash)]
        )
        self.assertEqual(state.blocks, tuple(base))
        self.assertEqual(state.unresolved[0]["reason"], "target_quote_missing_or_ambiguous")

    def test_add_preserves_baseline_and_unclassified_requires_question(self):
        base = blocks("base", b"First control.\n\nSecond control.\n")
        delta = blocks("delta", b"New feature.\n\nUnclear change.\n")
        state = resolve(base, delta, [Operation("op1", "add", delta[0].id)])
        self.assertEqual(
            [b.text for b in state.blocks], [base[0].text, base[1].text, delta[0].text]
        )
        self.assertEqual(state.unresolved[0]["reason"], "unclassified_delta")
        self.assertEqual((base[1].start_line, base[1].end_line), (3, 3))

    def test_replace_delete_require_exact_anchor_and_do_not_mutate_original(self):
        base = blocks("base", b"Auth required.\n\nAudit enabled.\n")
        delta = blocks("delta", b"No auth.\n\nDelete audit.\n")
        ops = [
            Operation("r", "replace", delta[0].id, base[0].id, base[0].hash),
            Operation("d", "delete", delta[1].id, base[1].id, "incorrect"),
        ]
        state = resolve(base, delta, ops)
        self.assertEqual({b.text for b in state.blocks}, {"No auth.\n", "Audit enabled.\n"})
        self.assertEqual(state.unresolved[0]["reason"], "target_hash_mismatch")
        self.assertEqual(base[0].text, "Auth required.\n")

    def test_derived_text_cannot_masquerade_as_original_evidence(self):
        base = blocks("base", b"Auth required.\n")
        delta = blocks("delta", b"Change auth.\n")
        state = resolve(
            base,
            delta,
            [Operation("r", "replace", delta[0].id, base[0].id, base[0].hash, "No auth.")],
        )
        self.assertEqual(state.blocks, tuple(base))
        self.assertEqual(state.unresolved[0]["reason"], "unanchored_replacement")

    def test_competing_changes_never_partially_apply(self):
        base = blocks("base", b"Control.\n")
        delta = blocks("delta", b"First.\n\nSecond.\n")
        ops = [
            Operation("a", "delete", delta[0].id, base[0].id, base[0].hash),
            Operation("b", "replace", delta[1].id, base[0].id, base[0].hash, "Changed"),
        ]
        state = resolve(base, delta, ops)
        self.assertEqual(state.blocks, tuple(base))
        self.assertEqual(len(state.unresolved), 2)


if __name__ == "__main__":
    unittest.main()
