"""Adoption review proposal builder."""

from __future__ import annotations

from ...domain import ReviewProposal, ReviewProposalItem


def build_adoption_proposal(*, cases: list[dict]) -> ReviewProposal:
    items = [
        ReviewProposalItem(
            target_id=str(c["id"]),
            action="adopt",
            rationale=f"suggest adopt: {c.get('title') or c['id']}",
            confidence=0.55,
        )
        for c in cases
        if str(c.get("review_status") or "pending") == "pending"
    ]
    return ReviewProposal(scope="adoption", items=items)
