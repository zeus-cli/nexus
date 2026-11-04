
"""Internal API for managing HITL approval requests."""

from __future__ import annotations

from typing import Any, Optional
from dataclasses import dataclass
from .approval import ApprovalManager, ApprovalRequest, ApprovalDecision, ApprovalStatus

@dataclass(frozen=True)
class APIUser:
    user_id: str
    name: str
    role: str

class ApprovalAPI:
    """API wrapper for ApprovalManager to handle authenticated human decisions."""

    def __init__(self, approval_manager: ApprovalManager) -> None:
        self._manager = approval_manager

    def list_pending(self) -> list[dict[str, Any]]:
        """Lists all requests awaiting human decision."""
        return self._manager.list_pending()

    def get_details(self, request_id: str) -> dict[str, Any] | None:
        """Returns full details of a specific request."""
        return self._manager.get_request(request_id)

    def approve(self, request_id: str, user: APIUser) -> bool:
        """Approves a request on behalf of an authenticated user."""
        try:
            decision = ApprovalDecision(
                decision="APPROVED",
                approver=user.user_id
            )
            self._manager.decide(request_id, decision)
            return True
        except ValueError:
            return False

    def deny(self, request_id: str, user: APIUser) -> bool:
        """Denies a request on behalf of an authenticated user."""
        try:
            decision = ApprovalDecision(
                decision="DENIED",
                approver=user.user_id
            )
            self._manager.decide(request_id, decision)
            return True
        except ValueError:
            return False

    def revoke(self, request_id: str, user: APIUser) -> bool:
        """Revokes an approval that hasn't been consumed yet."""
        try:
            self._manager.revoke(request_id)
            return True
        except ValueError:
            return False

    def get_history(self, limit: int = 100, status: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        """Returns history of decided approvals."""
        # This requires a new method in ApprovalManager
        # I will add it to ApprovalManager in a moment.
        return self._manager.get_history(limit=limit, status=status, offset=offset)