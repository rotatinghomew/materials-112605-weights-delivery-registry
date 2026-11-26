"""订单兑现：按用途、辖区与材料族组合可交割包。

一份可交割包的实际组成以 weights 登记为准：权重、适配包、基座和
摘要都取自登记记录。订单可以选择覆盖范围相同的替代组合，也可以
指定整套交割；替换必须同时满足材料族覆盖、基座兼容和订单允许替换。
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from itertools import combinations

from .clock import format_time, parse_time
from .errors import ConflictError, PolicyError, ValidationError
from .lineage import Entitlement, entitlement, obligations
from .model import Order, Snapshot, Weight
from .review import AuditResult, Selection, base_compatible, effective_approval


def pack_composition(snapshot: Snapshot, weights: tuple[Weight, ...]) -> dict:
    """按登记记录描述这一包的组成。"""
    digests = []
    files = []
    for weight in weights:
        digests.append(weight.sha256)
        files.append({"weight": weight.id, "file": weight.file, "sha256": weight.sha256})
    return {
        "weights": [weight.id for weight in weights],
        "digests": digests,
        "files": files,
        "adapters": [weight.id for weight in weights if weight.base_id is not None],
        "bases": [weight.id for weight in weights if weight.base_id is None],
    }


def delivery_credential(order: Order, claim: str, weights: tuple[Weight, ...], at: str) -> str:
    """当次交割凭据：绑定订单、认领号、具体字节摘要与业务时间。"""
    body = {
        "order": order.id,
        "claim": claim,
        "files": [weight.id for weight in weights],
        "digests": [weight.sha256 for weight in weights],
        "at": format_time(parse_time(at)),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Alternative:
    weights: tuple[Weight, ...]
    entitlements: tuple[Entitlement, ...]
    exact: bool

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(weight.id for weight in self.weights)

    def to_dict(self, snapshot: Snapshot, order: Order, at: str) -> dict:
        details = []
        for weight, result in zip(self.weights, self.entitlements):
            details.append({
                "weight": weight.id,
                "base_id": weight.base_id,
                "coverage": list(weight.coverage),
                "sha256": weight.sha256,
                "file": weight.file,
                "authorized": result.allowed,
                "grants": [grant.to_dict() for grant in result.grants],
            })
        return {
            "weights": list(self.ids),
            "coverage": sorted({family for weight in self.weights for family in weight.coverage}),
            "exact_coverage": self.exact,
            "whole_set": len(self.weights) == 1 and self.exact,
            "composition": pack_composition(snapshot, self.weights),
            "items": details,
        }


def _authorized(snapshot, weight: Weight, order: Order, instant) -> Entitlement:
    return entitlement(snapshot, weight.id, order.use, order.zone, format_time(instant))


def alternatives(snapshot: Snapshot, order: Order, at) -> list[Alternative]:
    """列出覆盖订单全部材料族、基座兼容且当时被授权的组合。

    优先少包且覆盖恰好等于订单要求的组合；只有没有恰好覆盖时才退而
    接受超集覆盖，但会把 ``exact`` 标成 False 供审查方判断。
    """
    instant = parse_time(at)
    usable: list[Weight] = []
    for weight in snapshot.weights:
        if not base_compatible(weight, order.existing_base):
            continue
        if not _authorized(snapshot, weight, order, instant).allowed:
            continue
        usable.append(weight)

    required = set(order.families)
    exact: list[Alternative] = []
    wider: list[Alternative] = []
    for size in range(1, len(usable) + 1):
        for combo in combinations(range(len(usable)), size):
            weights = tuple(usable[index] for index in combo)
            coverage = {family for weight in weights for family in weight.coverage}
            if not required <= coverage:
                continue
            entitlements = tuple(_authorized(snapshot, weight, order, instant) for weight in weights)
            item = Alternative(weights=weights, entitlements=entitlements,
                               exact=coverage == required)
            (exact if item.exact else wider).append(item)
        if exact:
            break
    chosen = exact or (wider[:1] if wider else [])
    # 同覆盖、同包数时只保留一个代表，但保留不同组成供客户选择。
    seen: set[tuple[str, ...]] = set()
    result = []
    for item in chosen:
        key = tuple(sorted(item.ids))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


@dataclass(frozen=True)
class Plan:
    order: Order
    at: str
    approved: AuditResult | None
    delivery: Alternative | None
    substituted: bool
    reasons: tuple[dict, ...]

    def to_dict(self, snapshot: Snapshot) -> dict:
        return {
            "order": self.order.id,
            "receiver": self.order.receiver,
            "zone": self.order.zone,
            "use": self.order.use,
            "families": list(self.order.families),
            "existing_base": self.order.existing_base,
            "replace_allowed": self.order.replace,
            "at": self.at,
            "status": "ready" if self.delivery else "blocked",
            "approved": self.approved.to_dict() if self.approved else None,
            "substituted": self.substituted,
            "delivery": self.delivery.to_dict(snapshot, self.order, self.at) if self.delivery else None,
            "reasons": list(self.reasons),
        }


def plan(snapshot: Snapshot, order_id: str, at) -> Plan:
    order = snapshot.by_id("orders", order_id)
    instant = parse_time(at)
    approval = effective_approval(snapshot, order.id, at)
    approved_alt: Alternative | None = None
    if approval is not None and approval.selection is not None:
        approved_alt = Alternative(
            weights=approval.selection.weights,
            entitlements=approval.selection.entitlements,
            exact=approval.selection.exact,
        )

    reasons: list[dict] = []
    # 被批准的具体组合即使当前没有其它可选替代，也要能按批准交付。
    options = alternatives(snapshot, order, instant)
    if approved_alt is not None and not options:
        options = [approved_alt]
    if approved_alt is not None:
        stale: list[str] = []
        for weight in approved_alt.weights:
            result = _authorized(snapshot, weight, order, instant)
            if not result.allowed:
                stale.append(weight.id)
        if not stale:
            return Plan(order=order, at=format_time(instant), approved=approval,
                        delivery=approved_alt, substituted=False, reasons=())
        reasons.append({"reason": "approval_not_authorized", "weights": stale})

    if options:
        if approved_alt is None:
            reasons.append({"reason": "no_effective_approval"})
        if not order.replace:
            reasons.append({"reason": "replacement_not_allowed", "order": order.id})
            return Plan(order=order, at=format_time(instant), approved=approval,
                        delivery=None, substituted=False, reasons=tuple(reasons))
        reasons.append({"reason": "substituted_alternative", "weights": list(options[0].ids)})
        return Plan(order=order, at=format_time(instant), approved=approval,
                    delivery=options[0], substituted=True, reasons=tuple(reasons))

    reasons.append({
        "reason": "no_covered_combination",
        "required": list(order.families),
        "replace_allowed": order.replace,
    })
    return Plan(order=order, at=format_time(instant), approved=approval,
                delivery=None, substituted=False, reasons=tuple(reasons))


def plans(snapshot: Snapshot, at) -> list[dict]:
    return [plan(snapshot, order.id, at).to_dict(snapshot) for order in snapshot.orders]


def versions(snapshot: Snapshot, order_id: str, at) -> dict:
    """客户从订单查询可用版本、可交付组合以及允许使用的地方。"""
    order = snapshot.by_id("orders", order_id)
    current = plan(snapshot, order.id, at)
    listed = []
    for option in alternatives(snapshot, order, at):
        item = option.to_dict(snapshot, order, at)
        uses: dict[str, list[str]] = {}
        for weight, result in zip(option.weights, option.entitlements):
            zones = sorted({
                grant.zone for grant in result.grants
            })
            uses[weight.id] = zones
        item["allowed_zones"] = uses
        listed.append(item)
    return {
        "order": order.id,
        "receiver": order.receiver,
        "zone": order.zone,
        "use": order.use,
        "families": list(order.families),
        "at": format_time(parse_time(at)),
        "status": current.to_dict(snapshot)["status"],
        "delivery": current.to_dict(snapshot)["delivery"],
        "alternatives": listed,
        "allowed_zones": sorted({order.zone}),
    }


def check_switch(snapshot: Snapshot, order_id: str, weights: tuple[str, ...], at) -> dict:
    """核实订单从批准版本改到替代组合时仍满足的三项条件。"""
    order = snapshot.by_id("orders", order_id)
    instant = parse_time(at)
    records = [snapshot.by_id("weights", key) for key in weights]
    coverage = {family for weight in records for family in weight.coverage}
    checks = {
        "coverage": sorted(coverage) == sorted(order.families),
        "coverage_missing": sorted(set(order.families) - coverage),
        "base_compatible": all(base_compatible(weight, order.existing_base) for weight in records),
        "replace_allowed": order.replace,
        "authorized": all(
            entitlement(snapshot, weight.id, order.use, order.zone, instant).allowed
            for weight in records
        ),
    }
    checks["acceptable"] = all(
        checks[key] for key in ("coverage", "base_compatible", "replace_allowed", "authorized")
    )
    return {
        "order": order.id,
        "at": format_time(instant),
        "weights": list(weights),
        "checks": checks,
    }
