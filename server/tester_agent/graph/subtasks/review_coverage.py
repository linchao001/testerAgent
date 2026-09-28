"""Coverage review proposal builder (heuristic; LLM optional later)."""

from __future__ import annotations

from ...domain import ReviewProposal, ReviewProposalItem


def build_coverage_proposal(
    *,
    uncovered_clause_ids: list[str],
    matrix_ref: str | None = None,
    degraded: bool = False,
) -> ReviewProposal:
    items = [
        ReviewProposalItem(
            target_id=cid,
            action="add_case",
            rationale=f"clause {cid} uncovered in matrix",
            confidence=0.7,
        )
        for cid in uncovered_clause_ids
    ]
    return ReviewProposal(
        scope="coverage",
        items=items,
        matrix_ref=matrix_ref,
        degraded=degraded or bool(uncovered_clause_ids),
    )
