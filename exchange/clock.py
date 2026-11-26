"""业务时间与到达时间的解析规则。

交换资料里的时间必须带时区偏移。业务判断一律使用 ``happened``，
``arrived`` 只决定对接方何时能提供该回执，不能代替业务发生时间。
"""
from __future__ import annotations

from datetime import datetime


def parse_time(value: str | datetime) -> datetime:
    """把 ISO-8601 文本解析成带时区的时间；拒绝无偏移的本地时间。"""
    if isinstance(value, datetime):
        instant = value
    else:
        if not isinstance(value, str) or not value:
            raise ValueError("时间须为带时区偏移的 ISO-8601 文本")
        instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("时间须包含时区偏移")
    return instant


def format_time(instant: datetime) -> str:
    """输出保留原时区偏移的 ISO-8601 文本。"""
    return instant.isoformat()


def latest(*instants: datetime) -> datetime:
    return max(instants)
