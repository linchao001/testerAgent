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


async def run_review_coverage(*, uncovered_clause_ids: list[str], matrix_ref: str | None = None) -> dict:
    """Subtask entry: return proposal payload + output_ref placeholder."""
    proposal = build_coverage_proposal(
        uncovered_clause_ids=uncovered_clause_ids,
        matrix_ref=matrix_ref,
    )
    return {
        "summary": f"coverage review: {len(proposal.items)} items",
        "proposal": proposal.model_dump(),
        "output_ref": matrix_ref or "art-review-coverage",
    }
