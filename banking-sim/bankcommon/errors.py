"""Error types. Every error body carries ``failure_path`` so the root cause is traceable."""

from __future__ import annotations


class BankError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 failure_path: list[str] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.failure_path = failure_path or []

    def body(self, service: str, correlation_id: str = "") -> dict:
        return {"error": {"code": self.code, "message": self.message, "service": service,
                          "failure_path": self.failure_path or [service],
                          "correlation_id": correlation_id}}


class DependencyUnavailable(BankError):
    def __init__(self, service: str, target: str, reason: str,
                 nested: list[str] | None = None) -> None:
        super().__init__(503, "DEPENDENCY_UNAVAILABLE", f"{target}: {reason}",
                         [service, *(nested or [target])])
        self.target, self.reason = target, reason


class UndeclaredDependency(RuntimeError):
    """A service tried to call something missing from dependency_map.json."""
