"""交换业务异常。"""
from __future__ import annotations


class ExchangeError(Exception):
    """所有交换业务错误的基类。"""

    code = "exchange_error"

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


class ValidationError(ExchangeError):
    code = "validation_error"


class IntegrityError(ExchangeError):
    code = "integrity_error"


class NotFoundError(ExchangeError):
    code = "not_found"


class PolicyError(ExchangeError):
    code = "policy_error"


class ConflictError(ExchangeError):
    code = "conflict"


class UnavailableError(ExchangeError):
    code = "unavailable"
