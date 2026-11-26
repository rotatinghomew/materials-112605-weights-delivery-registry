"""材料模型权重许可交割。

对外主要入口：

- :func:`exchange.model.load` 读取并校验交换资料
- :func:`exchange.lineage.entitlement` 判断权重在用途/辖区/时刻能否交付
- :func:`exchange.review.evaluate` 依据送审摘要与生效授权给出批准
- :func:`exchange.fulfilment.plan` 规划订单交付与替代适配包
- :class:`exchange.registry.Ledger` 记录客户侧交割与撤销处置
- :mod:`exchange.sync` 合并对接方回执
"""
from .clock import parse_time
from .errors import (
    ConflictError,
    ExchangeError,
    IntegrityError,
    NotFoundError,
    PolicyError,
    UnavailableError,
    ValidationError,
)
from .lineage import (
    affected_weights,
    all_boundaries,
    ancestors,
    boundary,
    entitlement,
    inherits_license,
    describe_lineage,
    obligations,
    trainable,
)
from .model import load
from .review import all_audits, approvals, evaluate, effective_approval
from .fulfilment import alternatives, check_switch, plan, plans, versions
from .registry import Delivery, Ledger, seal
from .sync import fetch, merge

__all__ = [
    "ConflictError",
    "Delivery",
    "ExchangeError",
    "IntegrityError",
    "Ledger",
    "NotFoundError",
    "PolicyError",
    "UnavailableError",
    "ValidationError",
    "affected_weights",
    "all_audits",
    "all_boundaries",
    "alternatives",
    "ancestors",
    "approvals",
    "boundary",
    "check_switch",
    "effective_approval",
    "entitlement",
    "evaluate",
    "fetch",
    "inherits_license",
    "describe_lineage",
    "load",
    "merge",
    "obligations",
    "parse_time",
    "plan",
    "plans",
    "seal",
    "trainable",
    "versions",
]
