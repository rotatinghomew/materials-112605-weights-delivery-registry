"""交割凭据、客户登记库与撤销后处置。

客户侧登记库先接收交割包，再按批准的监管辖区确认用户现在可取。
每一次交割都用绑定订单、认领号与具体字节摘要的凭据核对，因此双方
可以对同一凭据判断它是否仍处于允许请求或转交的状态。

状态机::

    issued ──confirmed──> dispatched ──staged──> staged ──approve──> enabled ──obtain──> obtained
       │                       │  \──unknown response──> staged_unconfirmed ──┘
       │                       │                    │
       └──unknown──> unknown ──┘                    └──revoke──> recalled / pending_verification

- ``issued``：本地已生成凭据，尚未得到对接方回应。
- ``unknown``：已发出请求但交割响应未知，包可能已进入外方管控。
- ``dispatched``：对接方确认收到这一次交割请求。
- ``staged``：登记库已接收交割包，但用户还不能取。
- ``staged_unconfirmed``：交割响应未知但登记库已收到包，保留外方管控可能。
- ``enabled``：已按批准辖区确认，用户可取。
- ``obtained``：用户已取到副本。
- ``recalled``：批准被撤销，已登记收回并停止再次取用。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path

from .clock import format_time, parse_time
from .errors import ConflictError, NotFoundError, PolicyError, ValidationError
from .fulfilment import delivery_credential, pack_composition
from .lineage import entitlement, obligations
from .model import Snapshot


ACTIVE_STATES = frozenset({"issued", "unknown", "dispatched", "staged", "staged_unconfirmed", "enabled", "obtained"})
TRANSFERABLE_STATES = frozenset({"issued", "unknown", "dispatched", "staged", "staged_unconfirmed", "enabled"})
PICKUP_STATES = frozenset({"enabled", "obtained"})


@dataclass(frozen=True)
class Delivery:
    id: str
    order: str
    claim: str
    receiver: str
    zone: str
    use: str
    weights: tuple[str, ...]
    digests: tuple[str, ...]
    state: str
    issued_at: str
    staged_at: str | None = None
    enabled_at: str | None = None
    obtained_at: str | None = None
    recalled_at: str | None = None
    note: str | None = None

    @property
    def package_key(self) -> tuple[str, ...]:
        return tuple(sorted(self.weights))

    def to_dict(self) -> dict:
        return {
            "credential": self.id,
            "order": self.order,
            "claim": self.claim,
            "receiver": self.receiver,
            "zone": self.zone,
            "use": self.use,
            "weights": list(self.weights),
            "digests": list(self.digests),
            "state": self.state,
            "issued_at": self.issued_at,
            "staged_at": self.staged_at,
            "enabled_at": self.enabled_at,
            "obtained_at": self.obtained_at,
            "recalled_at": self.recalled_at,
            "note": self.note,
            "transferable": self.state in TRANSFERABLE_STATES,
            "user_can_fetch": self.state == "enabled",
        }


@dataclass
class Ledger:
    """客户侧登记库。保存业务事件，便于审计与伙伴最小可见性。"""

    snapshot: Snapshot
    deliveries: dict[str, Delivery] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    # ---- 查询 -----------------------------------------------------------
    def get(self, credential: str) -> Delivery:
        try:
            return self.deliveries[credential]
        except KeyError as error:
            raise NotFoundError(f"未知交割凭据 {credential}", credential=credential) from error

    def for_order(self, order_id: str) -> list[Delivery]:
        self.snapshot.by_id("orders", order_id)
        return [item for item in self.deliveries.values() if item.order == order_id]

    def active_for_order(self, order_id: str) -> list[Delivery]:
        return [item for item in self.for_order(order_id) if item.state in ACTIVE_STATES]

    def by_claim(self, claim: str) -> list[Delivery]:
        return [item for item in self.deliveries.values() if item.claim == claim]

    # ---- 内部 -----------------------------------------------------------
    def _log(self, action: str, at: str, **detail) -> None:
        self.events.append({"action": action, "at": at, "detail": detail})

    def _replace(self, delivery: Delivery) -> None:
        self.deliveries[delivery.id] = delivery

    def _conflict_package(self, delivery: Delivery) -> Delivery | None:
        for other in self.deliveries.values():
            if other.id == delivery.id:
                continue
            if other.state not in TRANSFERABLE_STATES:
                continue
            if other.claim == delivery.claim:
                continue
            if other.package_key == delivery.package_key and other.order == delivery.order:
                return other
        return None

    # ---- 转移 -----------------------------------------------------------
    def issue(self, order_id: str, claim: str, weights: tuple[str, ...], at: str) -> Delivery:
        """生成一次交割凭据；此时尚未向对接方发出请求。"""
        order = self.snapshot.by_id("orders", order_id)
        if not claim:
            raise ValidationError("认领号不能为空")
        if not weights:
            raise ValidationError("交割包至少要包含一个工件")
        records = tuple(self.snapshot.by_id("weights", key) for key in weights)
        instant = parse_time(at)
        for record in records:
            result = entitlement(self.snapshot, record.id, order.use, order.zone, instant)
            if not result.allowed:
                raise PolicyError(
                    f"工件 {record.id} 在 {order.zone}/{order.use} 于该时刻未被授权",
                    weight=record.id,
                    denials=[denial.to_dict() for denial in result.denials],
                )
        credential = delivery_credential(order, claim, records, at)
        existing = self.deliveries.get(credential)
        if existing is not None:
            return existing
        delivery = Delivery(
            id=credential,
            order=order.id,
            claim=claim,
            receiver=order.receiver,
            zone=order.zone,
            use=order.use,
            weights=tuple(weight.id for weight in records),
            digests=tuple(weight.sha256 for weight in records),
            state="issued",
            issued_at=format_time(instant),
        )
        other = self._conflict_package(delivery)
        if other is not None:
            raise ConflictError(
                f"认领 {other.claim} 已持有同一排他包的可用凭据，不能同时再交付一份",
                credential=other.id,
                claim=other.claim,
                weights=list(other.weights),
            )
        self._replace(delivery)
        self._log("issue", delivery.issued_at, credential=credential, order=order.id,
                  claim=claim, weights=list(delivery.weights))
        return delivery

    def mark_unknown(self, credential: str, at: str) -> Delivery:
        """交割请求已发出但响应未知；保留包可能已进入外方管控的可能性。"""
        delivery = self.get(credential)
        if delivery.state not in {"issued", "unknown"}:
            raise ConflictError(f"凭据 {credential} 当前为 {delivery.state}，不能标记为响应未知",
                                credential=credential, state=delivery.state)
        updated = replace(delivery, state="unknown", note="交割响应未知")
        self._replace(updated)
        self._log("mark_unknown", format_time(parse_time(at)), credential=credential)
        return updated

    def confirm_dispatch(self, credential: str, at: str) -> Delivery:
        delivery = self.get(credential)
        if delivery.state == "dispatched":
            return delivery
        if delivery.state not in {"issued", "unknown"}:
            raise ConflictError(f"凭据 {credential} 当前为 {delivery.state}，不能确认交割",
                                credential=credential, state=delivery.state)
        updated = replace(delivery, state="dispatched", note=None)
        self._replace(updated)
        self._log("confirm_dispatch", format_time(parse_time(at)), credential=credential)
        return updated

    def stage(self, credential: str, at: str) -> Delivery:
        """客户登记库接收交割包；此时用户还不能取。"""
        delivery = self.get(credential)
        if delivery.state in {"staged", "enabled", "obtained"}:
            return delivery
        if delivery.state in {"issued", "recalled", "withdrawn", "pending_verification"}:
            raise ConflictError(f"凭据 {credential} 处于 {delivery.state}，不能接收",
                                credential=credential, state=delivery.state)
        stamp = format_time(parse_time(at))
        if delivery.state == "dispatched":
            state = "staged"
        elif delivery.state == "unknown":
            # 未收到对接方回复但包已到手：仍按曾进入外方管控的可能登记。
            state = "staged_unconfirmed"
        else:
            raise ConflictError(f"凭据 {credential} 处于 {delivery.state}，不能接收",
                                credential=credential, state=delivery.state)
        updated = replace(delivery, state=state, staged_at=stamp)
        self._replace(updated)
        self._log("stage", updated.staged_at, credential=credential)
        return updated

    def enable(self, credential: str, at: str) -> Delivery:
        """按批准的监管辖区确认用户现在可取。"""
        delivery = self.get(credential)
        if delivery.state == "enabled":
            return delivery
        if delivery.state not in {"staged", "staged_unconfirmed"}:
            raise ConflictError(f"凭据 {credential} 须先由登记库接收才能确认可取",
                                credential=credential, state=delivery.state)
        instant = parse_time(at)
        order = self.snapshot.by_id("orders", delivery.order)
        if delivery.zone != order.zone:
            raise PolicyError("确认辖区与订单批准辖区不一致", credential=credential,
                              approved=order.zone, presented=delivery.zone)
        for weight_id in delivery.weights:
            result = entitlement(self.snapshot, weight_id, delivery.use, delivery.zone, instant)
            if not result.allowed:
                raise PolicyError(
                    f"工件 {weight_id} 在确认时已不在批准范围内",
                    credential=credential,
                    weight=weight_id,
                    denials=[denial.to_dict() for denial in result.denials],
                )
        updated = replace(delivery, state="enabled", enabled_at=format_time(instant))
        self._replace(updated)
        self._log("enable", updated.enabled_at, credential=credential, zone=delivery.zone)
        return updated

    def obtain(self, credential: str, at: str) -> Delivery:
        """用户取到副本；此后撤销批准需要登记收回。"""
        delivery = self.get(credential)
        if delivery.state == "obtained":
            return delivery
        if delivery.state != "enabled":
            raise ConflictError("用户只能在确认可取之后取件", credential=credential,
                                state=delivery.state)
        updated = replace(delivery, state="obtained", obtained_at=format_time(parse_time(at)))
        self._replace(updated)
        self._log("obtain", updated.obtained_at, credential=credential)
        return updated

    # ---- 撤销 -----------------------------------------------------------
    def revoke(self, credential: str, at: str, *, approval_revoked: bool = True) -> dict:
        """批准被撤销后按凭据现状处置。

        - 已取到副本：登记收回回执，并停止再次取用。
        - 尚未确认取件且交割响应未知：待核实，保留包曾进入外方管控的可能。
        - 尚未发出或仅待接收：直接作废，不产生收回义务。
        """
        delivery = self.get(credential)
        stamp = format_time(parse_time(at))
        if not approval_revoked:
            raise PolicyError("只有批准被撤销时才执行撤销处置", credential=credential)
        if delivery.state == "recalled":
            return {"credential": credential, "outcome": "recalled", "delivery": delivery.to_dict()}
        if delivery.state == "obtained":
            updated = replace(delivery, state="recalled", recalled_at=stamp,
                              note="已登记收回，停止再次取用")
            outcome = "recalled"
        elif delivery.state in {"unknown", "staged_unconfirmed"}:
            updated = replace(delivery, state="pending_verification", recalled_at=stamp,
                              note="未确认取件且交割响应未知，保留外方管控可能")
            outcome = "pending_verification"
        elif delivery.state == "issued":
            updated = replace(delivery, state="pending_verification", recalled_at=stamp,
                              note="请求未获回应，按未确认取件处理")
            outcome = "pending_verification"
        elif delivery.state in {"dispatched", "staged", "staged_unconfirmed", "enabled"}:
            updated = replace(delivery, state="withdrawn", recalled_at=stamp,
                              note="尚未取件，直接停止转交")
            outcome = "withdrawn"
        else:
            raise ConflictError(f"凭据 {credential} 处于 {delivery.state}，无需处置",
                                credential=credential, state=delivery.state)
        self._replace(updated)
        self._log("revoke", stamp, credential=credential, outcome=outcome,
                  previous=delivery.state)
        return {"credential": credential, "outcome": outcome, "delivery": updated.to_dict()}

    def apply_change(self, change_id: str, at: str) -> dict:
        """一条许可变动只处置确实继承该许可、且辖区/用途相交的凭据。"""
        change = self.snapshot.by_id("changes", change_id)
        license = self.snapshot.by_id("licenses", change.license)
        outcomes = []
        untouched = []
        for delivery in self.deliveries.values():
            if delivery.state not in ACTIVE_STATES:
                continue
            inherits = any(
                license.data in obligations(self.snapshot, weight_id)
                for weight_id in delivery.weights
            )
            in_scope = delivery.zone == change.zone and delivery.use == change.use
            if delivery.order:
                order = self.snapshot.by_id("orders", delivery.order)
                in_scope = in_scope and order.zone == change.zone and order.use == change.use
            if inherits and in_scope:
                outcomes.append(self.revoke(delivery.id, at))
            else:
                untouched.append(delivery.id)
        self._log("apply_change", format_time(parse_time(at)), change=change_id,
                  affected=len(outcomes), untouched=len(untouched))
        return {
            "change": change_id,
            "license": license.id,
            "zone": change.zone,
            "use": change.use,
            "outcomes": outcomes,
            "untouched": untouched,
        }

    def pending_verification(self) -> list[dict]:
        return [item.to_dict() for item in self.deliveries.values()
                if item.state == "pending_verification"]

    def freight_control_risk(self) -> list[dict]:
        """可能已进入外方管控的包：响应未知或待核实的凭据。"""
        return [
            {**item.to_dict(), "foreign_control_possible": item.zone not in {"CN"}}
            for item in self.deliveries.values()
            if item.state in {"unknown", "staged_unconfirmed", "pending_verification"}
        ]

    # ---- 双方核对 -------------------------------------------------------
    def state_for(self, credential: str) -> dict:
        delivery = self.get(credential)
        return {
            "credential": delivery.id,
            "claim": delivery.claim,
            "order": delivery.order,
            "weights": list(delivery.weights),
            "digests": list(delivery.digests),
            "state": delivery.state,
            "transferable": delivery.state in TRANSFERABLE_STATES,
            "requestable": delivery.state in {"issued", "unknown", "dispatched", "staged", "staged_unconfirmed"},
            "user_can_fetch": delivery.state == "enabled",
        }

    def reconcile(self, credential: str, presented_digests: list[str]) -> dict:
        """双方按当次交割凭据核对是否仍是同一个包，以及能否继续转交。"""
        delivery = self.get(credential)
        matches = list(delivery.digests) == list(presented_digests)
        result = {
            "credential": credential,
            "digest_matches": matches,
            "same_package": matches and list(delivery.weights) != [],
            "state": delivery.state,
            "transferable": delivery.state in TRANSFERABLE_STATES and matches,
            "reason": None,
        }
        if not matches:
            result["reason"] = "digest_mismatch"
        elif delivery.state not in TRANSFERABLE_STATES:
            result["reason"] = f"state_{delivery.state}"
        return result

    # ---- 最小可见性 -----------------------------------------------------
    def partner_view(self, owner: str) -> dict:
        """研发伙伴只看到与自己数据批有关的交割事实。"""
        batches = {batch.id for batch in self.snapshot.batches if batch.owner == owner}
        if not batches:
            raise NotFoundError(f"没有属于 {owner} 的数据批", owner=owner)
        facts = []
        for delivery in self.deliveries.values():
            related = {
                batch for weight_id in delivery.weights
                for batch in obligations(self.snapshot, weight_id) if batch in batches
            }
            if not related:
                continue
            facts.append({
                "credential": delivery.id,
                "order": delivery.order,
                "claim": delivery.claim,
                "weights": [weight_id for weight_id in delivery.weights
                            if related & set(obligations(self.snapshot, weight_id))],
                "batches": sorted(related),
                "state": delivery.state,
                "zone": delivery.zone,
                "use": delivery.use,
            })
        return {
            "owner": owner,
            "batches": sorted(batches),
            "deliveries": facts,
        }

    def customer_view(self, order_id: str) -> dict:
        order = self.snapshot.by_id("orders", order_id)
        deliveries = [item.to_dict() for item in self.for_order(order.id)]
        return {
            "order": order.id,
            "receiver": order.receiver,
            "zone": order.zone,
            "use": order.use,
            "families": list(order.families),
            "deliveries": deliveries,
        }

    # ---- 持久化 ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "deliveries": [item.to_dict() for item in self.deliveries.values()],
            "events": self.events,
        }

    @classmethod
    def from_dict(cls, snapshot: Snapshot, raw: dict) -> "Ledger":
        ledger = cls(snapshot=snapshot)
        for row in raw.get("deliveries", []):
            delivery = Delivery(
                id=row["credential"],
                order=row["order"],
                claim=row["claim"],
                receiver=row["receiver"],
                zone=row["zone"],
                use=row["use"],
                weights=tuple(row["weights"]),
                digests=tuple(row["digests"]),
                state=row["state"],
                issued_at=row["issued_at"],
                staged_at=row.get("staged_at"),
                enabled_at=row.get("enabled_at"),
                obtained_at=row.get("obtained_at"),
                recalled_at=row.get("recalled_at"),
                note=row.get("note"),
            )
            ledger.deliveries[delivery.id] = delivery
        ledger.events = list(raw.get("events", []))
        return ledger

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, snapshot: Snapshot, path: str | Path) -> "Ledger":
        return cls.from_dict(snapshot, json.loads(Path(path).read_text(encoding="utf-8")))


def seal(delivery: Delivery) -> str:
    """凭据的稳定摘要，便于双方比较同一次交割。"""
    body = json.dumps(
        {"order": delivery.order, "claim": delivery.claim,
         "weights": list(delivery.weights), "digests": list(delivery.digests)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return sha256(body).hexdigest()
