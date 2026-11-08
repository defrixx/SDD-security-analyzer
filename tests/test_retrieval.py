"""Recall and scope boundaries, including nonmatching explicit delta text."""

import unittest

from spec_enricher.effective_state import blocks
from spec_enricher.retrieval import ContextIndex


class RetrievalTests(unittest.TestCase):
    def test_some_lexical_matches_do_not_hide_other_selected_passages(self):
        selected = blocks(
            "A", b"authorization audit policy\n\nDomain ownership checks are removed.\n"
        )
        foreign = blocks("B", b"authorization FOREIGN PROJECT SECRET\n")
        index = ContextIndex(selected + foreign)
        try:
            found, trace = index.retrieve("authorization", allowed={b.id for b in selected})
            self.assertEqual({b.id for b in found}, {b.id for b in selected})
            self.assertNotIn(foreign[0].id, trace["retrieved_blocks"])
            self.assertFalse(trace["bounded"])
        finally:
            index.close()

    def test_changed_block_has_priority_within_the_same_explicit_budget(self):
        baseline = blocks("base", ("authorization " * 40).encode())
        delta = blocks("delta", b"Remove domain checks.\n")
        foreign = blocks("B", b"FOREIGN\n")
        index = ContextIndex(baseline + delta + foreign)
        try:
            found, trace = index.retrieve(
                "authorization",
                allowed={baseline[0].id, delta[0].id},
                max_bytes=40,
                priority_ids=[foreign[0].id, delta[0].id],
            )
            self.assertEqual(found, delta)
            self.assertTrue(trace["bounded"])
            self.assertEqual(trace["priority_blocks"], [delta[0].id])
            self.assertEqual(trace["omitted_priority_blocks"], [])
            self.assertEqual(found[0].hash, delta[0].hash)
        finally:
            index.close()

    def test_unread_priority_is_visible_and_no_block_is_silently_truncated(self):
        delta = blocks("delta", b"one\n\ntwo\n\nthree\n")
        index = ContextIndex(delta)
        try:
            found, trace = index.retrieve(
                "unmatched",
                allowed=set(index.blocks),
                max_bytes=8,
                limit=1,
                priority_ids=[b.id for b in delta],
            )
            self.assertEqual(found, delta[:1])
            self.assertEqual(trace["omitted_priority_blocks"], [b.id for b in delta[1:]])
            self.assertTrue(trace["bounded"])
        finally:
            index.close()
