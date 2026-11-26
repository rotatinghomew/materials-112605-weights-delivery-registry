"""交换资料的命令行查询与交割办理。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import fulfilment, lineage, review, sync
from .clock import parse_time
from .errors import ExchangeError, NotFoundError, ValidationError
from .model import Snapshot, load
from .registry import Ledger


DEFAULT_DATA = Path(__file__).resolve().parents[1] / "fixtures" / "exchange.json"


def emit(payload, *, stream=sys.stdout) -> None:
    json.dump(payload, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def _load(args) -> Snapshot:
    return load(args.data)


def cmd_validate(args) -> int:
    snapshot = _load(args)
    emit({
        "ok": True,
        "source": str(snapshot.source),
        "counts": {name: len(records) for name, records in snapshot.collections().items()},
        "warnings": list(snapshot.warnings),
    })
    return 0


def cmd_lineage(args) -> int:
    snapshot = _load(args)
    emit(lineage.describe_lineage(snapshot, args.weight))
    return 0


def cmd_entitlement(args) -> int:
    snapshot = _load(args)
    emit(lineage.entitlement(snapshot, args.weight, args.use, args.zone, args.at).to_dict())
    return 0


def cmd_boundary(args) -> int:
    snapshot = _load(args)
    if args.all:
        emit(lineage.all_boundaries(snapshot, args.at))
    elif args.weight:
        emit(lineage.boundary(snapshot, args.weight, args.at))
    else:
        raise ValidationError("请给出 --weight 或 --all")
    return 0


def cmd_trainable(args) -> int:
    snapshot = _load(args)
    emit(lineage.trainable(snapshot, args.weight, args.zone, args.at).to_dict())
    return 0


def cmd_audits(args) -> int:
    snapshot = _load(args)
    if args.audit:
        audit = snapshot.by_id("audits", args.audit)
        emit(review.evaluate(snapshot, audit).to_dict())
    else:
        emit([result.to_dict() for result in review.all_audits(snapshot)])
    return 0


def cmd_approvals(args) -> int:
    snapshot = _load(args)
    emit(review.approvals(snapshot, args.at))
    return 0


def cmd_plans(args) -> int:
    snapshot = _load(args)
    if args.order:
        emit(fulfilment.plan(snapshot, args.order, args.at).to_dict(snapshot))
    else:
        emit(fulfilment.plans(snapshot, args.at))
    return 0


def cmd_versions(args) -> int:
    snapshot = _load(args)
    emit(fulfilment.versions(snapshot, args.order, args.at))
    return 0


def cmd_check_switch(args) -> int:
    snapshot = _load(args)
    emit(fulfilment.check_switch(snapshot, args.order, tuple(args.weight), args.at))
    return 0


def cmd_changes(args) -> int:
    snapshot = _load(args)
    if args.change:
        emit(review.audit_impact(snapshot, args.change))
    else:
        emit([
            {
                "change": change.id,
                "license": change.license,
                "use": change.use,
                "zone": change.zone,
                "happened": change.happened,
                "affected_weights": lineage.affected_weights(snapshot, change.id),
                "affected_audits": review.audit_impact(snapshot, change.id)["affected_audits"],
            }
            for change in snapshot.changes
        ])
    return 0


def cmd_receipts(args) -> int:
    snapshot = _load(args)
    local = list(snapshot.receipts)
    if args.url:
        remote = sync.fetch(args.url, args.at, snapshot=snapshot)
    else:
        remote = []
    result = sync.merge(local, remote, args.at)
    emit(result.to_dict())
    return 0


def cmd_sync(args) -> int:
    snapshot = _load(args)
    remote = sync.fetch(args.url, args.at, snapshot=snapshot)
    result = sync.merge(list(snapshot.receipts), remote, args.at)
    emit(result.to_dict())
    return 0


def cmd_ledger(args) -> int:
    snapshot = _load(args)
    ledger = Ledger.load(snapshot, args.ledger) if args.ledger.exists() else Ledger(snapshot)
    action = args.action
    if action == "issue":
        if not args.claim or not args.weight:
            raise ValidationError("issue 需要 --claim 与 --weight")
        result = ledger.issue(args.order, args.claim, tuple(args.weight), args.at).to_dict()
    elif action == "unknown":
        result = ledger.mark_unknown(args.credential, args.at).to_dict()
    elif action == "dispatch":
        result = ledger.confirm_dispatch(args.credential, args.at).to_dict()
    elif action == "stage":
        result = ledger.stage(args.credential, args.at).to_dict()
    elif action == "enable":
        result = ledger.enable(args.credential, args.at).to_dict()
    elif action == "obtain":
        result = ledger.obtain(args.credential, args.at).to_dict()
    elif action == "revoke":
        result = ledger.revoke(args.credential, args.at)
    elif action == "apply-change":
        result = ledger.apply_change(args.change, args.at)
    elif action == "reconcile":
        if args.digest is None:
            raise ValidationError("reconcile 需要 --digest")
        result = ledger.reconcile(args.credential, args.digest)
    elif action == "show":
        result = ledger.state_for(args.credential)
    elif action == "pending":
        result = ledger.pending_verification()
    elif action == "risk":
        result = ledger.freight_control_risk()
    elif action == "partner":
        result = ledger.partner_view(args.owner)
    elif action == "customer":
        result = ledger.customer_view(args.order)
    elif action == "state":
        result = ledger.to_dict()
    else:
        raise ValidationError(f"未知动作 {action}")
    ledger.save(args.ledger)
    emit(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="exchange", description="材料模型权重许可交割")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="交换资料路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验交换资料")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("lineage", help="查看权重谱系与继承的数据义务")
    p.add_argument("weight")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("entitlement", help="判断权重在某用途/辖区/时刻能否交付")
    p.add_argument("weight")
    p.add_argument("--use", required=True)
    p.add_argument("--zone", required=True)
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_entitlement)

    p = sub.add_parser("boundary", help="注明许可边界")
    p.add_argument("--weight")
    p.add_argument("--all", action="store_true")
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_boundary)

    p = sub.add_parser("trainable", help="研究内用范围是否仍可继续训练")
    p.add_argument("weight")
    p.add_argument("--zone", required=True)
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_trainable)

    p = sub.add_parser("audits", help="逐条审查结果")
    p.add_argument("--audit")
    p.set_defaults(func=cmd_audits)

    p = sub.add_parser("approvals", help="各订单在某一时刻的生效批准")
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_approvals)

    p = sub.add_parser("plans", help="订单兑现方案")
    p.add_argument("--order")
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_plans)

    p = sub.add_parser("versions", help="客户查询订单可用版本与允许辖区")
    p.add_argument("order")
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_versions)

    p = sub.add_parser("check-switch", help="核实替代组合是否可接受")
    p.add_argument("order")
    p.add_argument("--weight", action="append", required=True)
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_check_switch)

    p = sub.add_parser("changes", help="许可变动影响范围")
    p.add_argument("--change")
    p.add_argument("--at", type=parse_time)
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("receipts", help="本地回执按业务时间排序")
    p.add_argument("--url")
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_receipts)

    p = sub.add_parser("sync", help="拉取对接方回执并与本地合并")
    p.add_argument("--url", required=True)
    p.add_argument("--at", required=True, type=parse_time)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ledger", help="客户登记库操作")
    p.add_argument("action", choices=[
        "issue", "unknown", "dispatch", "stage", "enable", "obtain", "revoke",
        "apply-change", "reconcile", "show", "pending", "risk", "partner", "customer", "state",
    ])
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--order")
    p.add_argument("--claim")
    p.add_argument("--weight", action="append")
    p.add_argument("--credential")
    p.add_argument("--change")
    p.add_argument("--digest", action="append")
    p.add_argument("--owner")
    p.add_argument("--at", type=parse_time)
    p.set_defaults(func=cmd_ledger)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ExchangeError as error:
        emit(error.to_dict(), stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
