"""对接方回执查询与登记回执合并。

对接方列表的先后不代表业务先后：合并时按 ``happened`` 排序，``arrived``
只决定某一时刻能看到什么。读到一个回执也不代表本地已做过相应操作，
因此合并只陈述事实，不推进客户侧状态。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.error
import urllib.parse
import urllib.request

from .clock import format_time, parse_time
from .errors import UnavailableError, ValidationError
from .model import RECEIPT_STATES, Receipt, Snapshot


def receipt_from_exchange(raw: dict, snapshot: Snapshot) -> Receipt:
    """把对接方返回的回执对象校对成内部记录，并验证摘要对应实际工件。"""
    required = ("id", "order", "claim", "files", "digests", "state", "happened", "arrived")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValidationError(f"回执缺少字段 {', '.join(missing)}", receipt=raw.get("id"))
    if raw["state"] not in RECEIPT_STATES:
        raise ValidationError(f"未知回执状态 {raw['state']}", receipt=raw.get("id"))
    snapshot.by_id("orders", raw["order"])
    if len(raw["files"]) != len(raw["digests"]):
        raise ValidationError("回执的文件与摘要数量不一致", receipt=raw["id"])
    for weight_id, digest in zip(raw["files"], raw["digests"]):
        if snapshot.by_id("weights", weight_id).sha256 != digest:
            raise ValidationError(f"回执 {raw['id']} 的摘要与登记工件 {weight_id} 不符",
                                  receipt=raw["id"], weight=weight_id)
    happened = parse_time(raw["happened"])
    arrived = parse_time(raw["arrived"])
    if happened > arrived:
        raise ValidationError("回执业务时间不能晚于到达时间", receipt=raw["id"])
    return Receipt(
        id=raw["id"], order=raw["order"], claim=raw["claim"],
        files=tuple(raw["files"]), digests=tuple(raw["digests"]),
        state=raw["state"], happened=raw["happened"], arrived=raw["arrived"],
    )


@dataclass
class SyncResult:
    at: str
    receipts: list[dict]

    def to_dict(self) -> dict:
        return {"at": self.at, "receipts": self.receipts}


def fetch(url: str, at: str, *, timeout: float = 5.0, snapshot: Snapshot | None = None) -> list[Receipt]:
    """查询某业务时间点可取得的回执。503 表示当前无法查询。"""
    instant = parse_time(at)
    query = urllib.parse.urlencode({"at": format_time(instant)})
    target = f"{url.rstrip('/')}/v1/receipts?{query}"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise UnavailableError("对接方暂时无法查阅回执", status=503) from error
        raise UnavailableError(f"回执查询失败：HTTP {error.code}", status=error.code) from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise UnavailableError(f"回执查询失败：{error}") from error
    rows = payload.get("receipts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise UnavailableError("对接方返回了无法识别的回执格式")
    if snapshot is None:
        return rows
    return [receipt_from_exchange(row, snapshot) for row in rows]


def merge(local: list[Receipt], remote: list[Receipt], at) -> SyncResult:
    """按业务时间合并两批回执；到达早于业务时间的不改变先后。"""
    instant = parse_time(at)
    seen: dict[str, Receipt] = {}
    for receipt in list(local) + list(remote):
        if parse_time(receipt.arrived) > instant:
            continue
        if parse_time(receipt.happened) > instant:
            continue
        existing = seen.get(receipt.id)
        if existing is not None and existing != receipt:
            raise ValidationError(f"同名回执 {receipt.id} 内容不一致，不能合并", receipt=receipt.id)
        seen[receipt.id] = receipt
    ordered = sorted(seen.values(), key=lambda item: (parse_time(item.happened), item.id))
    return SyncResult(at=format_time(instant), receipts=[item.__dict__ for item in ordered])
