"""Run-local FTS over explicitly selected project snapshots, without embeddings."""

from __future__ import annotations

import re
import sqlite3

from .effective_state import Block


class ContextIndex:
    def __init__(self, blocks: list[Block]):
        self.blocks = {block.id: block for block in blocks}
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE VIRTUAL TABLE context USING fts5(block_id UNINDEXED,body,tokenize='unicode61')"
        )
        self.connection.executemany(
            "INSERT INTO context(block_id,body) VALUES(?,?)",
            [(block.id, block.heading + "\n" + block.text) for block in blocks],
        )

    def close(self):
        self.connection.close()

    def retrieve(
        self,
        query: str,
        *,
        allowed: set[str],
        max_bytes: int = 16000,
        limit: int = 12,
        priority_ids=(),
    ):
        terms = list(dict.fromkeys(re.findall(r"[^\W_]{3,}", query.lower(), re.UNICODE)))[:64]
        expression = " OR ".join('"' + term + '"' for term in terms)
        found = (
            self.connection.execute(
                "SELECT block_id FROM context WHERE context MATCH ? ORDER BY bm25(context),block_id LIMIT 200",
                (expression,),
            ).fetchall()
            if expression
            else []
        )
        ranked = [row[0] for row in found if row[0] in allowed]
        priority = [
            key for key in dict.fromkeys(priority_ids) if key in allowed and key in self.blocks
        ]
        # Exact lexical matches must not hide an explicit changed block written
        # with different inflections/vocabulary. Fill remaining room from the
        # selected snapshots even when FTS already found some other passages.
        ordered = dict.fromkeys(priority + ranked + [key for key in self.blocks if key in allowed])
        candidates = [self.blocks[key] for key in ordered]
        result: list[Block] = []
        size = 0
        for block in candidates:
            block_size = len(block.text.encode())
            if len(result) >= limit or size + block_size > max_bytes:
                continue
            result.append(block)
            size += block_size
        return result, {
            "query": query,
            "terms": terms,
            "retrieved_blocks": [block.id for block in result],
            "reason": "explicit_priority_then_ranked_fts_then_selected_snapshot_fallback",
            "fts_candidate_count": len(ranked),
            "priority_blocks": priority,
            "omitted_priority_blocks": [
                key for key in priority if key not in {b.id for b in result}
            ],
            "candidate_count": len(candidates),
            "returned_count": len(result),
            "bounded": len(result) < len(candidates),
            "max_bytes": max_bytes,
        }
