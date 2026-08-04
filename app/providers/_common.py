"""providers 包内部共用的小工具，被 balances / coding_plans / mimo 共用。

单独成模块（而不是塞进 __init__.py）是为了避免循环 import——app/providers/
__init__.py 要 `from . import balances, coding_plans, mimo, ...` 把各 provider
模块登记进 REGISTRY；如果 _require 挂在 __init__.py 上，balances.py 等模块
反过来想用它就要 `from . import _require`（即 `from app.providers import
_require`），这会在 app.providers 包本身还没执行完 __init__.py（正卡在
`from . import balances` 这一行）的时候，被 balances.py 反向触发对同一个尚未
初始化完的包再次导入，形成循环 import。放在这个独立模块里，__init__.py 和
各 provider 模块都只需要 `from ._common import _require`（或 `from .. import`
不涉及），互相不依赖对方的初始化顺序，没有这个问题。
"""

from __future__ import annotations

from ..models import ChannelResult, fail


def _require(
    value: str | None,
    field_label: str,
    base: dict,
    *,
    message: str | None = None,
) -> ChannelResult | None:
    """必填字段缺失时的统一友好报错：缺失时返回一个 error 结果，否则返回 None。

    Pydantic 层（app/main.py）已经会在新建渠道时挡掉必填字段缺失的情况，但这里
    仍然兜底一层——避免 base_url=None 时拼出 "None/api/xxx" 这种丑陋 URL，或者
    sk=None 时报 "'NoneType' object has no attribute 'encode'" 这种没法排查的
    Python 内部异常（编辑已有渠道时密钥/base_url 是可以被显式清空成空值的，见
    config_store.upsert_channel 的 provided_fields 语义，所以"曾经必填校验通过"
    不代表"现在查询时一定还有值"）。

    message：自定义完整错误文案，覆盖默认的 "未配置 {field_label}"。个别渠道的
    缺失提示需要带操作指引（比如 MiMo 缺 Cookie 时要告诉用户去哪个页面复制哪个
    字段），不能被这里的通用模板文案覆盖掉，所以留了这个口子。
    """
    if not value:
        return fail("error", message or f"未配置 {field_label}", **base)
    return None
