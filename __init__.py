"""fnOS 管理插件 (fnos_cli)

N.E.K.O. 插件的加载入口。``plugin.toml`` 的 ``[plugin].entry`` 指向
``plugin.plugins.fnos_cli.plugin:FnOsCliPlugin``（子模块，便于静态校验），
本模块仍以 ``__getattr__`` 惰性转发同一个类。

运行时通过 ``__getattr__`` 惰性导入：裸 ``import fnos_cli`` 不会拉入
N.E.K.O. SDK，因此 ``tests/`` 里的冒烟测试可以在没有宿主 SDK 的环境下
直接运行 —— pytest 收集 ``tests/`` 时会先把插件根目录当成 package 导入，
根模块一旦在顶层 import ``plugin.sdk.plugin`` 就会失败。
"""

from typing import TYPE_CHECKING, Any

__all__ = ["FnOsCliPlugin"]

if TYPE_CHECKING:  # 仅供静态检查与类型提示，运行时不执行
    from .plugin import FnOsCliPlugin


def __getattr__(name: str) -> Any:
    if name == "FnOsCliPlugin":
        from .plugin import FnOsCliPlugin

        return FnOsCliPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
