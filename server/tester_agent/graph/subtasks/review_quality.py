"""Quality review proposal builder."""

from __future__ import annotations

from ...domain import ReviewProposal, ReviewProposalItem


def build_quality_proposal(*, case_issues: list[dict]) -> ReviewProposal:
    items = [
        ReviewProposalItem(
            target_id=str(issue["case_id"]),
            action="repair",
            rationale=str(issue.get("issue") or "quality issue"),
            confidence=float(issue.get("confidence", 0.6)),
        )
        for issue in case_issues
    ]
    return ReviewProposal(scope="quality", items=items)
