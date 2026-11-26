"""交换资料的实体模型与装载校验。

记录来自不同流程阶段。装载时只做结构、引用和工件字节校验，
业务判断（授权、覆盖、登记）放到 :mod:`exchange.ledger` 等模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from .clock import parse_time
from .errors import IntegrityError, NotFoundError, ValidationError


RECEIPT_STATES = frozenset({"staged", "obtained", "enabled", "no_further_access", "pending_verification"})
ACCESS_RECEIPT_STATES = frozenset({"obtained", "enabled"})


@dataclass(frozen=True)
class Batch:
    id: str
    material: str
    owner: str


@dataclass(frozen=True)
class License:
    id: str
    data: str
    uses: tuple[str, ...]
    zones: tuple[str, ...]
    begins: str
    expires: str


@dataclass(frozen=True)
class Weight:
    id: str
    base_id: str | None
    parents: tuple[str, ...]
    data: tuple[str, ...]
    coverage: tuple[str, ...]
    file: str
    sha256: str


@dataclass(frozen=True)
class Order:
    id: str
    receiver: str
    zone: str
    use: str
    families: tuple[str, ...]
    existing_base: str | None
    replace: bool


@dataclass(frozen=True)
class Audit:
    id: str
    reviewer: str
    order: str
    candidates: tuple[str, ...]
    happened: str


@dataclass(frozen=True)
class Change:
    id: str
    license: str
    use: str
    zone: str
    happened: str


@dataclass(frozen=True)
class Receipt:
    id: str
    order: str
    claim: str
    files: tuple[str, ...]
    digests: tuple[str, ...]
    state: str
    happened: str
    arrived: str


@dataclass(frozen=True)
class Snapshot:
    """一份交换资料的只读快照。"""

    root: Path
    source: Path
    batches: tuple[Batch, ...]
    licenses: tuple[License, ...]
    weights: tuple[Weight, ...]
    orders: tuple[Order, ...]
    audits: tuple[Audit, ...]
    changes: tuple[Change, ...]
    receipts: tuple[Receipt, ...]
    warnings: tuple[str, ...] = field(default=(), compare=False)

    def collections(self) -> dict[str, tuple]:
        return {
            "batches": self.batches,
            "licenses": self.licenses,
            "weights": self.weights,
            "orders": self.orders,
            "audits": self.audits,
            "changes": self.changes,
            "receipts": self.receipts,
        }

    def by_id(self, name: str, key: str):
        for record in self.collections()[name]:
            if record.id == key:
                return record
        raise NotFoundError(f"{name} 中不存在记录 {key}", collection=name, id=key)

    def latest_happened(self) -> str:
        instants: list[str] = []
        instants += [r.happened for r in self.audits]
        instants += [r.happened for r in self.changes]
        instants += [r.happened for r in self.receipts]
        instants += [r.arrived for r in self.receipts]
        return max(instants, key=parse_time) if instants else "1970-01-01T00:00:00+00:00"


def _require(record: dict, keys: Iterable[str], name: str) -> None:
    missing = [key for key in keys if key not in record]
    if missing:
        raise ValidationError(
            f"{name} 记录缺少字段 {', '.join(missing)}",
            collection=name,
            missing=missing,
            record=record.get("id"),
        )


def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValidationError(f"{field_name} 须为非空字符串", field=field_name)
    return value


def _texts(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{field_name} 须为非空字符串列表", field=field_name)
    if not all(isinstance(item, str) and item for item in value):
        raise ValidationError(f"{field_name} 只能包含非空字符串", field=field_name)
    if len(set(value)) != len(value):
        raise ValidationError(f"{field_name} 不能包含重复项", field=field_name)
    return tuple(value)


def _unique_ids(records: list, name: str) -> dict[str, object]:
    index: dict[str, object] = {}
    for record in records:
        if record.id in index:
            raise ValidationError(f"{name} 中记录 id 重复：{record.id}", collection=name, id=record.id)
        index[record.id] = record
    return index


def _parse_batches(rows: object) -> list[Batch]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("batches 须为非空列表", collection="batches")
    result = []
    for row in rows:
        _require(row, ("id", "material", "owner"), "batches")
        result.append(Batch(
            id=_text(row["id"], "batches.id"),
            material=_text(row["material"], "batches.material"),
            owner=_text(row["owner"], "batches.owner"),
        ))
    return result


def _parse_licenses(rows: object) -> list[License]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("licenses 须为非空列表", collection="licenses")
    result = []
    for row in rows:
        _require(row, ("id", "data", "uses", "zones", "begins", "expires"), "licenses")
        license = License(
            id=_text(row["id"], "licenses.id"),
            data=_text(row["data"], "licenses.data"),
            uses=_texts(row["uses"], "licenses.uses"),
            zones=_texts(row["zones"], "licenses.zones"),
            begins=_text(row["begins"], "licenses.begins"),
            expires=_text(row["expires"], "licenses.expires"),
        )
        if parse_time(license.begins) >= parse_time(license.expires):
            raise ValidationError(f"许可 {license.id} 的生效时间必须早于到期时间", license=license.id)
        result.append(license)
    return result


def _parse_weights(rows: object) -> list[Weight]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("weights 须为非空列表", collection="weights")
    result = []
    for row in rows:
        _require(row, ("id", "base_id", "parents", "data", "coverage", "file", "sha256"), "weights")
        base = row["base_id"]
        if base is not None and (not isinstance(base, str) or not base):
            raise ValidationError("weights.base_id 须为空的基座标记或记录 id", weight=row.get("id"))
        result.append(Weight(
            id=_text(row["id"], "weights.id"),
            base_id=base,
            parents=_texts_or_empty(row["parents"], "weights.parents"),
            data=_texts_or_empty(row["data"], "weights.data"),
            coverage=_texts(row["coverage"], "weights.coverage"),
            file=_text(row["file"], "weights.file"),
            sha256=_text(row["sha256"], "weights.sha256"),
        ))
    return result


def _texts_or_empty(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} 须为字符串列表", field=field_name)
    if not all(isinstance(item, str) and item for item in value):
        raise ValidationError(f"{field_name} 只能包含非空字符串", field=field_name)
    if len(set(value)) != len(value):
        raise ValidationError(f"{field_name} 不能包含重复项", field=field_name)
    return tuple(value)


def _parse_orders(rows: object) -> list[Order]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("orders 须为非空列表", collection="orders")
    result = []
    for row in rows:
        _require(row, ("id", "receiver", "zone", "use", "families", "existing_base", "replace"), "orders")
        existing = row["existing_base"]
        if existing is not None and (not isinstance(existing, str) or not existing):
            raise ValidationError("orders.existing_base 须为空或权重 id", order=row.get("id"))
        if not isinstance(row["replace"], bool):
            raise ValidationError("orders.replace 须为布尔值", order=row.get("id"))
        result.append(Order(
            id=_text(row["id"], "orders.id"),
            receiver=_text(row["receiver"], "orders.receiver"),
            zone=_text(row["zone"], "orders.zone"),
            use=_text(row["use"], "orders.use"),
            families=_texts(row["families"], "orders.families"),
            existing_base=existing,
            replace=row["replace"],
        ))
    return result


def _parse_audits(rows: object) -> list[Audit]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("audits 须为非空列表", collection="audits")
    result = []
    for row in rows:
        _require(row, ("id", "reviewer", "order", "candidates", "happened"), "audits")
        parse_time(row["happened"])
        result.append(Audit(
            id=_text(row["id"], "audits.id"),
            reviewer=_text(row["reviewer"], "audits.reviewer"),
            order=_text(row["order"], "audits.order"),
            candidates=_texts(row["candidates"], "audits.candidates"),
            happened=_text(row["happened"], "audits.happened"),
        ))
    return result


def _parse_changes(rows: object) -> list[Change]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("changes 须为非空列表", collection="changes")
    result = []
    for row in rows:
        _require(row, ("id", "license", "use", "zone", "happened"), "changes")
        parse_time(row["happened"])
        result.append(Change(
            id=_text(row["id"], "changes.id"),
            license=_text(row["license"], "changes.license"),
            use=_text(row["use"], "changes.use"),
            zone=_text(row["zone"], "changes.zone"),
            happened=_text(row["happened"], "changes.happened"),
        ))
    return result


def _parse_receipts(rows: object) -> list[Receipt]:
    if not isinstance(rows, list) or not rows:
        raise ValidationError("receipts 须为非空列表", collection="receipts")
    result = []
    for row in rows:
        _require(row, ("id", "order", "claim", "files", "digests", "state", "happened", "arrived"), "receipts")
        if row["state"] not in RECEIPT_STATES:
            raise ValidationError(f"未知的登记状态：{row['state']}", receipt=row.get("id"))
        happened = parse_time(row["happened"])
        arrived = parse_time(row["arrived"])
        if happened > arrived:
            raise ValidationError("回执的业务发生时间不能晚于到达时间", receipt=row.get("id"))
        result.append(Receipt(
            id=_text(row["id"], "receipts.id"),
            order=_text(row["order"], "receipts.order"),
            claim=_text(row["claim"], "receipts.claim"),
            files=_texts(row["files"], "receipts.files"),
            digests=_texts(row["digests"], "receipts.digests"),
            state=row["state"],
            happened=row["happened"],
            arrived=row["arrived"],
        ))
    return result


def _check_references(snapshot: Snapshot) -> list[str]:
    """引用、覆盖和工件字节校验；返回非致命提醒。"""
    warnings: list[str] = []
    batches = _unique_ids(list(snapshot.batches), "batches")
    licenses = _unique_ids(list(snapshot.licenses), "licenses")
    weights = _unique_ids(list(snapshot.weights), "weights")
    orders = _unique_ids(list(snapshot.orders), "orders")
    _unique_ids(list(snapshot.audits), "audits")
    _unique_ids(list(snapshot.changes), "changes")
    _unique_ids(list(snapshot.receipts), "receipts")

    for license in snapshot.licenses:
        if license.data not in batches:
            raise IntegrityError(f"许可 {license.id} 指向未知数据批 {license.data}", license=license.id)
    for weight in snapshot.weights:
        for batch_id in weight.data:
            if batch_id not in batches:
                raise IntegrityError(f"权重 {weight.id} 引用了未知数据批 {batch_id}", weight=weight.id)
        for parent in weight.parents:
            if parent not in weights:
                raise IntegrityError(f"权重 {weight.id} 引用了未知上游检查点 {parent}", weight=weight.id)
            if parent == weight.id:
                raise IntegrityError(f"权重 {weight.id} 不能以自身为上游", weight=weight.id)
        if weight.base_id is not None:
            if weight.base_id not in weights:
                raise IntegrityError(f"权重 {weight.id} 的基座 {weight.base_id} 不存在", weight=weight.id)
            base = weights[weight.base_id]
            if base.base_id is not None:
                raise IntegrityError(f"权重 {weight.id} 的基座 {base.id} 本身不是基座", weight=weight.id)
            if weight.base_id in weight.parents:
                continue
            warnings.append(f"权重 {weight.id} 声明基座 {weight.base_id} 但未列其为上游检查点")
        _check_artifact(snapshot, weight)
    _check_cycles(snapshot)

    for order in snapshot.orders:
        if order.existing_base is not None and order.existing_base not in weights:
            raise IntegrityError(f"订单 {order.id} 的既有基座不存在：{order.existing_base}", order=order.id)
        known_families = {family for weight in snapshot.weights for family in weight.coverage}
        for family in order.families:
            if family not in known_families:
                raise IntegrityError(f"订单 {order.id} 要求的材料族没有对应工件：{family}", order=order.id)
    for audit in snapshot.audits:
        if audit.order not in orders:
            raise IntegrityError(f"审查 {audit.id} 指向未知订单 {audit.order}", audit=audit.id)
        for candidate in audit.candidates:
            if candidate not in weights:
                raise IntegrityError(f"审查 {audit.id} 送审了未知工件 {candidate}", audit=audit.id)
    for change in snapshot.changes:
        if change.license not in licenses:
            raise IntegrityError(f"许可变动 {change.id} 指向未知许可 {change.license}", change=change.id)
        license = licenses[change.license]
        if change.use == "internal":
            raise ValidationError("内用授权不受地区转授撤销的影响，变动记录不能指向 internal", change=change.id)
        if change.use not in license.uses:
            raise IntegrityError(f"变动 {change.id} 的用途不在许可 {license.id} 范围内", change=change.id)
        if change.zone not in license.zones:
            raise IntegrityError(f"变动 {change.id} 的辖区不在许可 {license.id} 范围内", change=change.id)
    for receipt in snapshot.receipts:
        if receipt.order not in orders:
            raise IntegrityError(f"回执 {receipt.id} 指向未知订单 {receipt.order}", receipt=receipt.id)
        if len(receipt.files) != len(receipt.digests):
            raise IntegrityError(f"回执 {receipt.id} 的文件与摘要数量不一致", receipt=receipt.id)
        for weight_id, digest in zip(receipt.files, receipt.digests):
            if weight_id not in weights:
                raise IntegrityError(f"回执 {receipt.id} 指称未知工件 {weight_id}", receipt=receipt.id)
            if weights[weight_id].sha256 != digest:
                raise IntegrityError(
                    f"回执 {receipt.id} 的摘要与登记工件 {weight_id} 的字节不符",
                    receipt=receipt.id,
                    weight=weight_id,
                )
    return warnings


def _check_artifact(snapshot: Snapshot, weight: Weight) -> None:
    path = snapshot.root / weight.file
    if not path.is_file():
        raise IntegrityError(f"权重 {weight.id} 的工件不存在：{weight.file}", weight=weight.id)
    actual = sha256(path.read_bytes()).hexdigest()
    if actual != weight.sha256:
        raise IntegrityError(
            f"权重 {weight.id} 的登记摘要与工件字节不符",
            weight=weight.id,
            registered=weight.sha256,
            actual=actual,
        )


def _check_cycles(snapshot: Snapshot) -> None:
    parents = {weight.id: weight.parents for weight in snapshot.weights}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, trail: tuple[str, ...]) -> None:
        if node in done:
            return
        if node in visiting:
            raise IntegrityError(f"上游检查点形成环：{' -> '.join(trail + (node,))}", weight=node)
        visiting.add(node)
        for parent in parents.get(node, ()):
            visit(parent, trail + (node,))
        visiting.discard(node)
        done.add(node)

    for weight_id in parents:
        visit(weight_id, ())


def load(path: str | Path, root: str | Path | None = None) -> Snapshot:
    """读取并校验交换资料。工件路径相对于资料所在仓库根目录。"""
    source = Path(path).resolve()
    base = Path(root).resolve() if root is not None else source.parent.parent
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValidationError(f"交换资料不是合法 JSON：{error}", source=str(source)) from error
    if not isinstance(raw, dict):
        raise ValidationError("交换资料顶层须为对象", source=str(source))
    required = ("batches", "licenses", "weights", "orders", "audits", "changes", "receipts")
    missing = [name for name in required if name not in raw]
    if missing:
        raise ValidationError(f"交换资料缺少集合 {', '.join(missing)}", missing=missing)
    snapshot = Snapshot(
        root=base,
        source=source,
        batches=tuple(_parse_batches(raw["batches"])),
        licenses=tuple(_parse_licenses(raw["licenses"])),
        weights=tuple(_parse_weights(raw["weights"])),
        orders=tuple(_parse_orders(raw["orders"])),
        audits=tuple(_parse_audits(raw["audits"])),
        changes=tuple(_parse_changes(raw["changes"])),
        receipts=tuple(_parse_receipts(raw["receipts"])),
        warnings=(),
    )
    warnings = _check_references(snapshot)
    return Snapshot(**{**snapshot.__dict__, "warnings": tuple(warnings)})
