"""向对接方发出交割请求。

接收只代表到达对手方，不会生成履约回执；连接中断时应视为交割响应
未知，由登记库保留包可能已进入外方管控的可能性。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.request

from .errors import ValidationError


@dataclass(frozen=True)
class DispatchResult:
    outcome: str  # accepted / unknown
    status: int | None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "status": self.status, "detail": self.detail}


def build_payload(credential: str, delivery, *, material_families=()) -> dict:
    return {
        "credential": credential,
        "order": delivery.order,
        "claim": delivery.claim,
        "receiver": delivery.receiver,
        "zone": delivery.zone,
        "use": delivery.use,
        "weights": list(delivery.weights),
        "digests": list(delivery.digests),
        "families": list(material_families),
        "at": delivery.issued_at,
    }


def dispatch(url: str, payload: dict, *, timeout: float = 5.0) -> DispatchResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/dispatches", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status in (200, 201, 202):
                return DispatchResult(outcome="accepted", status=response.status)
            return DispatchResult(outcome="unknown", status=response.status)
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500:
            raise ValidationError(f"对接方拒收交割请求：HTTP {error.code}", status=error.code) from error
        return DispatchResult(outcome="unknown", status=error.code, detail=str(error))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        return DispatchResult(outcome="unknown", status=None, detail=str(error))
