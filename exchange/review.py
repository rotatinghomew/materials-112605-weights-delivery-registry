"""发布审查与批准。

审查员依据送审的具体字节摘要和生效授权批准。批准记录一组具体的
权重包组成，而不是"某个材料族的某个版本"，因此后续换用替代适配包
必须重新核对覆盖、基座兼容与订单的替换许可。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

from .clock import format_time, parse_time
from .errors import PolicyError, ValidationError
from .lineage import Entitlement, entitlement, obligations
from .model import Audit, Order, Snapshot, Weight


def base_compatible(weight: Weight, existing_base: str | None) -> bool:
    """适配包只能装在所给基座上；基座本身可充当整包。"""
    if existing_base is None:
        return True
    if weight.id == existing_base:
        return True
    return weight.base_id == existing_base


def _coverage(weights: Iterable[Weight]) -> frozenset[str]:
    return frozenset(family for weight in weights for family in weight.coverage)


def _entitlements(snapshot: Snapshot, weights: Iterable[Weight], order: Order, at) -> list[Entitlement]:
    return [entitlement(snapshot, weight.id, order.use, order.zone, at) for weight in weights]


@dataclass(frozen=True)
class Selection:
    weights: tuple[Weight, ...]
    entitlements: tuple[Entitlement, ...]
    coverage: frozenset[str]
    exact: bool

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(weight.id for weight in self.weights)

    @property
    def digests(self) -> tuple[str, ...]:
        return tuple(weight.sha256 for weight in self.weights)

    def to_dict(self) -> dict:
        return {
            "weights": list(self.ids),
            "digests": list(self.digests),
            "coverage": sorted(self.coverage),
            "exact_coverage": self.exact,
        }


def _covering_sets(snapshot: Snapshot, order: Order, candidates: Iterable[Weight], at) -> list[Selection]:
    required = set(order.families)
    usable: list[Weight] = []
    for weight in candidates:
        if not base_compatible(weight, order.existing_base):
            continue
        result = entitlement(snapshot, weight.id, order.use, order.zone, at)
        if result.allowed:
            usable.append(weight)
    # 先找覆盖恰好等于订单要求的组合；只有在没有这种组合时才接受更大的
    # 超集，并把超出的材料族如实标进 reasons，交由审查方判断。
    exact: list[Selection] = []
    wider: list[Selection] = []
    # 优先少包、再优先候选给出顺序，避免无谓地把整条链塞进交割。
    for size in range(1, len(usable) + 1):
        for combo in combinations(range(len(usable)), size):
            weights = tuple(usable[index] for index in combo)
            coverage = _coverage(weights)
            if not required <= coverage:
                continue
            entitlements = tuple(
                entitlement(snapshot, weight.id, order.use, order.zone, at) for weight in weights
            )
            item = Selection(
                weights=weights,
                entitlements=entitlements,
                coverage=coverage,
                exact=coverage == frozenset(required),
            )
            (exact if item.exact else wider).append(item)
        if exact:
            break
    if exact:
        return exact
    if wider:
        return sorted(wider, key=lambda item: len(item.coverage))[:1]
    return []


def _dedupe(selections: Iterable[Selection]) -> list[Selection]:
    seen: set[tuple[str, ...]] = set()
    result = []
    for selection in selections:
        key = tuple(sorted(selection.ids))
        if key in seen:
            continue
        seen.add(key)
        result.append(selection)
    return result


@dataclass(frozen=True)
class AuditResult:
    audit: Audit
    order: Order
    approved: bool
    selection: Selection | None
    reasons: tuple[dict, ...]
    approval: dict | None

    def to_dict(self) -> dict:
        return {
            "audit": self.audit.id,
            "reviewer": self.audit.reviewer,
            "order": self.audit.order,
            "happened": self.audit.happened,
            "approved": self.approved,
            "approval": self.approval,
            "reasons": list(self.reasons),
        }


def evaluate(snapshot: Snapshot, audit: Audit) -> AuditResult:
    order = snapshot.by_id("orders", audit.order)
    candidates = [snapshot.by_id("weights", key) for key in audit.candidates]
    at = parse_time(audit.happened)
    reasons: list[dict] = []

    missing_base = [w.id for w in candidates if not base_compatible(w, order.existing_base)]
    if missing_base:
        reasons.append({"reason": "incompatible_base", "weights": missing_base,
                        "existing_base": order.existing_base})
    denied: list[dict] = []
    for weight in candidates:
        result = entitlement(snapshot, weight.id, order.use, order.zone, at)
        if not result.allowed:
            denied.append({
                "reason": "not_authorized",
                "weight": weight.id,
                "denials": [denial.to_dict() for denial in result.denials],
            })
    if denied:
        reasons.append({"reason": "candidate_not_authorized", "candidates": denied})

    selections = _dedupe(_covering_sets(snapshot, order, candidates, at))
    if not selections:
        covered = sorted(_coverage(candidates))
        reasons.append({
            "reason": "incomplete_coverage",
            "required": sorted(order.families),
            "candidates_cover": covered,
        })
        return AuditResult(audit=audit, order=order, approved=False, selection=None, reasons=tuple(reasons), approval=None)

    selection = selections[0]
    seen_batches: set[str] = set()
    if not selection.exact:
        reasons.append({
            "reason": "extra_coverage",
            "required": sorted(order.families),
            "selected_cover": sorted(selection.coverage),
        })
    approval = {
        "audit": audit.id,
        "reviewer": audit.reviewer,
        "order": order.id,
        "weights": list(selection.ids),
        "digests": list(selection.digests),
        "coverage": sorted(selection.coverage),
        "existing_base": order.existing_base,
        "use": order.use,
        "zone": order.zone,
        "authorized_at": audit.happened,
        "grants": [
            grant.to_dict()
            for result in selection.entitlements
            for grant in result.grants
            if grant.batch not in seen_batches
            and not seen_batches.add(grant.batch)
        ],
    }
    return AuditResult(audit=audit, order=order, approved=True, selection=selection,
                       reasons=tuple(reasons), approval=approval)


def all_audits(snapshot: Snapshot) -> list[AuditResult]:
    return [evaluate(snapshot, audit) for audit in snapshot.audits]


def _revoked_at(snapshot: Snapshot, result: AuditResult) -> str | None:
    """批准本身仍在有效期内，但其组成的部分工件随后失去授权的时间。

    这里只看批准所钉住的那些数据批，且只算与订单用途、辖区相交的变动。
    其它辖区的撤销、或与这批数据无关的修订，不会让批准失效。
    """
    assert result.selection is not None
    order = result.order
    batches = {
        batch for weight in result.selection.weights
        for batch in obligations(snapshot, weight.id)
    }
    stamps: list[str] = []
    for change in snapshot.changes:
        if change.use != order.use or change.zone != order.zone:
            continue
        license = snapshot.by_id("licenses", change.license)
        if license.data not in batches:
            continue
        moment = parse_time(change.happened)
        if moment <= parse_time(result.audit.happened):
            continue
        if not _license_covers(snapshot, license, order.use, order.zone, moment):
            continue
        stamps.append(change.happened)
    return min(stamps, key=parse_time) if stamps else None


def _license_covers(snapshot: Snapshot, license, use: str, zone: str, at) -> bool:
    if use not in license.uses or zone not in license.zones:
        return False
    if at < parse_time(license.begins) or at >= parse_time(license.expires):
        return False
    return True


def _still_authorized(snapshot: Snapshot, result: AuditResult, at) -> bool:
    """批准当时存在且现在仍不被撤销的授权，才算在 ``at`` 生效。

    拒绝使用后来才签署、只在后来生效的许可来支撑当时的批准。
    """
    assert result.selection is not None
    order = result.order
    issued = parse_time(result.audit.happened)
    denied_at_issue = [
        item for item in result.selection.entitlements if not item.allowed
    ]
    if denied_at_issue:
        return False
    for item in result.selection.entitlements:
        if item.zone != order.zone or item.use != order.use:
            return False
    revoked = _revoked_at(snapshot, result)
    if revoked is not None and parse_time(revoked) <= parse_time(at):
        return False
    return True


def effective_approval(snapshot: Snapshot, order_id: str, at) -> AuditResult | None:
    """返回在该业务时间仍然生效、且最晚作出的一次批准。

    批准钉住具体的字节摘要；后来被撤销的批准自然失效，但不相关的
    许可变动不会动到别的订单。不采用晚于 ``at`` 的审查。
    另外返回 ``valid_from``，即批准因撤销或到期而失效的最早时刻。
    """
    instant = parse_time(at)
    valid = [
        result for result in all_audits(snapshot)
        if result.order.id == order_id
        and result.approved
        and parse_time(result.audit.happened) <= instant
        and _still_authorized(snapshot, result, instant)
    ]
    if not valid:
        return None
    return max(valid, key=lambda result: parse_time(result.audit.happened))



def audit_impact(snapshot: Snapshot, change_id: str) -> dict:
    """一条许可变动会牵动哪些审查；只算确实继承该许可且作用域相交的。"""
    change = snapshot.by_id("changes", change_id)
    license = snapshot.by_id("licenses", change.license)
    affected = []
    unaffected = []
    for result in all_audits(snapshot):
        uses_license = any(
            license.data in snapshot_batches_of(snapshot, weight.id)
            for weight in (result.selection.weights if result.selection else ())
        ) or any(
            license.data in snapshot_batches_of(snapshot, candidate)
            for candidate in result.audit.candidates
        )
        in_scope = result.order.use == change.use and result.order.zone == change.zone
        if uses_license and in_scope:
            affected.append(result.audit.id)
        else:
            unaffected.append(result.audit.id)
    return {
        "change": change_id,
        "license": license.id,
        "use": change.use,
        "zone": change.zone,
        "affected_audits": affected,
        "unaffected_audits": unaffected,
    }


def snapshot_batches_of(snapshot: Snapshot, weight_id: str) -> tuple[str, ...]:
    from .lineage import obligations
    return obligations(snapshot, weight_id)


def approvals(snapshot: Snapshot, at) -> list[dict]:
    result = []
    for order in snapshot.orders:
        effective = effective_approval(snapshot, order.id, at)
        result.append({
            "order": order.id,
            "receiver": order.receiver,
            "zone": order.zone,
            "use": order.use,
            "families": list(order.families),
            "effective_approval": effective.to_dict() if effective else None,
        })
    return result
