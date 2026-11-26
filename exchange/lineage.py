"""权重谱系、许可继承与许可边界。

交付限制不只看权重自己直接读取的批次：继续训练会继承上游检查点的
数据义务，基座也不例外。缺少元数据引用不能证明义务消失，因此谱系
只依据 ``parents`` / ``base_id`` 推导，而不依赖 ``data`` 是否还列着某批。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .clock import format_time, parse_time
from .errors import NotFoundError
from .model import License, Snapshot, Weight


@dataclass(frozen=True)
class Grant:
    batch: str
    license: str
    use: str
    zone: str

    def to_dict(self) -> dict:
        return {"batch": self.batch, "license": self.license, "use": self.use, "zone": self.zone}


@dataclass(frozen=True)
class Denial:
    batch: str
    reason: str
    licenses: tuple[str, ...] = ()
    change: str | None = None

    def to_dict(self) -> dict:
        return {
            "batch": self.batch,
            "reason": self.reason,
            "licenses": list(self.licenses),
            "change": self.change,
        }


@dataclass(frozen=True)
class Entitlement:
    weight: str
    use: str
    zone: str
    at: str
    allowed: bool
    grants: tuple[Grant, ...]
    denials: tuple[Denial, ...]
    obligations: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "weight": self.weight,
            "use": self.use,
            "zone": self.zone,
            "at": self.at,
            "allowed": self.allowed,
            "grants": [grant.to_dict() for grant in self.grants],
            "denials": [denial.to_dict() for denial in self.denials],
            "obligations": list(self.obligations),
        }


def ancestors(snapshot: Snapshot, weight_id: str) -> tuple[Weight, ...]:
    """按由近及远、去重后的顺序返回权重及其全部上游检查点。"""
    weights = {weight.id: weight for weight in snapshot.weights}
    if weight_id not in weights:
        raise NotFoundError(f"未知权重 {weight_id}", weight=weight_id)
    ordered: list[Weight] = []
    seen: set[str] = set()

    def walk(key: str) -> None:
        if key in seen:
            return
        weight = weights.get(key)
        if weight is None:
            raise NotFoundError(f"未知上游检查点 {key}", weight=key)
        seen.add(key)
        ordered.append(weight)
        for parent in weight.parents:
            walk(parent)
        if weight.base_id is not None:
            walk(weight.base_id)

    walk(weight_id)
    return tuple(ordered)


def obligations(snapshot: Snapshot, weight_id: str) -> tuple[str, ...]:
    """权重交付必须满足的全部数据批，含未直接读取的上游批次。"""
    batches: list[str] = []
    for weight in ancestors(snapshot, weight_id):
        for batch in weight.data:
            if batch not in batches:
                batches.append(batch)
    return tuple(sorted(batches))


def describe_lineage(snapshot: Snapshot, weight_id: str) -> dict:
    weight = snapshot.by_id("weights", weight_id)
    chain = ancestors(snapshot, weight_id)
    owned = obligations(snapshot, weight_id)
    direct = set(weight.data)
    inherited = [batch for batch in owned if batch not in direct]
    return {
        "weight": weight_id,
        "base": weight.base_id,
        "parents": list(weight.parents),
        "direct_batches": list(weight.data),
        "inherited_batches": inherited,
        "obligations": list(owned),
        "chain": [item.id for item in chain],
        "coverage": list(weight.coverage),
        "digest": weight.sha256,
        "artifacts": [item.file for item in chain],
    }


def _changes_against(snapshot: Snapshot, license_id: str, use: str, zone: str, at: datetime):
    result = []
    for change in snapshot.changes:
        if change.license != license_id or change.use != use or change.zone != zone:
            continue
        if parse_time(change.happened) <= at:
            result.append(change)
    return result


def license_state(snapshot: Snapshot, license: License, use: str, zone: str, at: datetime) -> tuple[bool, str, str | None]:
    """返回（是否可用, 原因, 相关变动 id）。"""
    if use not in license.uses or zone not in license.zones:
        return False, "not_in_scope", None
    revoked = _changes_against(snapshot, license.id, use, zone, at)
    if revoked:
        return False, "revoked", revoked[-1].id
    begins = parse_time(license.begins)
    expires = parse_time(license.expires)
    if at < begins:
        return False, "not_yet_effective", None
    if at >= expires:
        return False, "expired", None
    return True, "granted", None


def entitlement(snapshot: Snapshot, weight_id: str, use: str, zone: str, at: str | datetime) -> Entitlement:
    """判断权重在给定用途、辖区与业务时间是否可交付。"""
    instant = parse_time(at)
    required = obligations(snapshot, weight_id)
    by_batch: dict[str, list[License]] = {}
    for license in snapshot.licenses:
        by_batch.setdefault(license.data, []).append(license)
    grants: list[Grant] = []
    denials: list[Denial] = []
    for batch in required:
        candidates = by_batch.get(batch, [])
        best: Grant | None = None
        reasons: list[Denial] = []
        for license in candidates:
            ok, reason, change = license_state(snapshot, license, use, zone, instant)
            if ok:
                best = Grant(batch=batch, license=license.id, use=use, zone=zone)
                break
            reasons.append(Denial(batch=batch, reason=reason, licenses=(license.id,), change=change))
        if best is not None:
            grants.append(best)
        elif reasons:
            # 优先报告撤销/过期等最具体的原因。
            priority = {"revoked": 0, "expired": 1, "not_yet_effective": 2, "not_in_scope": 3}
            denials.append(sorted(reasons, key=lambda item: priority.get(item.reason, 9))[0])
        else:
            denials.append(Denial(batch=batch, reason="no_license"))
    return Entitlement(
        weight=weight_id,
        use=use,
        zone=zone,
        at=format_time(instant),
        allowed=not denials,
        grants=tuple(grants),
        denials=tuple(denials),
        obligations=required,
    )


def uses_and_zones(snapshot: Snapshot, weight_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required = set(obligations(snapshot, weight_id))
    uses: list[str] = []
    zones: list[str] = []
    for license in snapshot.licenses:
        if license.data not in required:
            continue
        for use in license.uses:
            if use not in uses:
                uses.append(use)
        for zone in license.zones:
            if zone not in zones:
                zones.append(zone)
    return tuple(sorted(uses)), tuple(sorted(zones))


def boundary(snapshot: Snapshot, weight_id: str, at: str | datetime) -> dict:
    """注明权重的许可边界：每种用途/辖区是否仍被允许及其原因。"""
    instant = parse_time(at)
    uses, zones = uses_and_zones(snapshot, weight_id)
    matrix: dict[str, dict[str, dict]] = {}
    for use in uses:
        matrix[use] = {}
        for zone in zones:
            result = entitlement(snapshot, weight_id, use, zone, instant)
            matrix[use][zone] = {
                "allowed": result.allowed,
                "denials": [denial.to_dict() for denial in result.denials],
            }
    return {
        "weight": weight_id,
        "at": format_time(instant),
        "coverage": list(snapshot.by_id("weights", weight_id).coverage),
        "obligations": list(obligations(snapshot, weight_id)),
        "uses": matrix,
        "base": snapshot.by_id("weights", weight_id).base_id,
        "digest": snapshot.by_id("weights", weight_id).sha256,
    }


def trainable(snapshot: Snapshot, weight_id: str, zone: str, at: str | datetime) -> Entitlement:
    """研究内用范围仍然保留时，权重可继续进入下一轮训练。"""
    return entitlement(snapshot, weight_id, "internal", zone, at)


def inherits_license(snapshot: Snapshot, weight_id: str, license_id: str) -> bool:
    license = snapshot.by_id("licenses", license_id)
    return license.data in obligations(snapshot, weight_id)


def affected_weights(snapshot: Snapshot, change_id: str) -> list[str]:
    """一条许可变动实际影响到的权重（只含确实继承该许可的包）。"""
    change = snapshot.by_id("changes", change_id)
    license = snapshot.by_id("licenses", change.license)
    return [
        weight.id
        for weight in snapshot.weights
        if license.data in obligations(snapshot, weight.id)
    ]


def all_boundaries(snapshot: Snapshot, at: str | datetime) -> dict[str, dict]:
    return {weight.id: boundary(snapshot, weight.id, at) for weight in snapshot.weights}
