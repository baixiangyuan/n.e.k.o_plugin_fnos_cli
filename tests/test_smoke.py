"""fnos_cli 插件冒烟测试：清单、入口与纯函数逻辑。

这些测试不调用 fnos-cli，也不连接设备，只验证插件自身的静态契约。
所有断言都基于文件内容与 AST，不 import 插件模块 —— 插件依赖宿主提供的
`plugin.sdk.plugin`，在独立 pytest 环境里不可用。
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
# 实现模块：plugin.toml 的 [plugin].entry 指向 plugin.py:FnOsCliPlugin。
# 根目录 __init__.py 只是惰性转发的门面（裸 import 不拉宿主 SDK），
# 因此所有静态检查都针对 plugin.py。
ENTRY_MODULE = PLUGIN_DIR / "plugin.py"


def _load_manifest() -> dict:
    with (PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        return tomllib.load(handle)


def _parse_entry_module() -> ast.Module:
    return ast.parse(ENTRY_MODULE.read_text(encoding="utf-8"))


def _entry_ids() -> set[str]:
    """收集 @plugin_entry(id=...) 声明的入口 ID。"""
    found: set[str] = set()
    for node in ast.walk(_parse_entry_module()):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if getattr(decorator.func, "attr", getattr(decorator.func, "id", "")) != "plugin_entry":
                continue
            # 运行时入口必须是 async，同步入口会在触发时失败。
            assert isinstance(node, ast.AsyncFunctionDef), f"entry {node.name} must be async"
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    found.add(keyword.value.value)
    return found


def _ui_context_ids() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_parse_entry_module()):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if getattr(decorator.func, "attr", "") != "context":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    found.add(keyword.value.value)
    return found


def _module_dict(name: str) -> dict[str, object]:
    """在不执行模块的前提下，读取 `NAME = {...}` 形式的模块级字典字面量。"""
    for node in _parse_entry_module().body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
    return {}


# ── 清单 ──────────────────────────────────────────────────────────────────


def test_plugin_toml_is_valid() -> None:
    manifest = _load_manifest()
    plugin = manifest["plugin"]
    assert plugin["id"] == "fnos_cli"
    assert plugin["id"] == PLUGIN_DIR.name
    assert plugin["name"]
    assert plugin["version"]
    assert plugin["type"] == "plugin"
    assert re.fullmatch(r"[A-Za-z0-9_.\-]+:[A-Za-z_][A-Za-z0-9_]*", plugin["entry"])


def test_manifest_declares_surfaces_and_files_exist() -> None:
    manifest = _load_manifest()
    ui = manifest["plugin"]["ui"]
    assert ui["enabled"] is True

    panel = ui["panel"][0]
    assert panel["id"] == "main"
    assert panel["context"] == "dashboard"
    assert panel["entry"].endswith(".tsx")
    assert (PLUGIN_DIR / panel["entry"]).is_file()
    for permission in panel["permissions"]:
        assert permission in {"state:read", "config:read", "config:write",
                              "action:call", "logs:read", "runs:read"}

    guide = ui["guide"][0]
    assert guide["entry"].endswith(".md")
    assert (PLUGIN_DIR / guide["entry"]).is_file()

    locales = manifest["plugin"]["i18n"]
    assert (PLUGIN_DIR / locales["locales_dir"] / "zh-CN.json").is_file()


def test_locale_files_are_valid_json_and_share_keys() -> None:
    locales_dir = PLUGIN_DIR / "locales"
    payloads = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(locales_dir.glob("*.json"))
    }
    assert payloads, "no locale files found"

    baseline = payloads["zh-CN.json"]
    for name, payload in payloads.items():
        assert payload["plugin.name"], f"{name} missing plugin.name"
        assert set(baseline) - set(payload) == set(), f"{name} missing keys"


def test_launcher_assets_are_pinned() -> None:
    manifest = json.loads((PLUGIN_DIR / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert manifest["product"] == "fnos"
    assert manifest["version"]

    scripts = PLUGIN_DIR / "scripts"
    assert (scripts / "fnos-cli").is_file()
    assert (scripts / "fnos-cli.cmd").is_file()
    assert (scripts / "fnos-cli.ps1").is_file()

    for platform, asset in manifest["assets"].items():
        assert platform
        assert asset["name"] and asset["sha256"] and asset["binarySha256"]
        assert asset["size"] > 0 and asset["binarySize"] > 0


# ── 入口与 UI ─────────────────────────────────────────────────────────────


def test_entry_module_declares_expected_entries() -> None:
    entries = _entry_ids()
    assert {
        "status", "login_begin", "login_finish", "logout", "run",
        "system_info", "storage_overview", "storage_pools", "storage_disks",
        "monitor_cpu", "monitor_memory", "download_list", "docker_stats",
    } <= entries
    # 登录只走专用入口，交互式 login 不开放。
    assert "login" not in entries
    # 入口 ID 在插件内唯一（AST 扫描用集合，重复会被合并，这里显式查重）。
    seen: list[str] = []
    for node in ast.walk(_parse_entry_module()):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if getattr(decorator.func, "attr", getattr(decorator.func, "id", "")) != "plugin_entry":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    seen.append(keyword.value.value)
    assert len(seen) == len(set(seen)), f"duplicate entry ids: {seen}"


def test_dashboard_ui_context_is_declared() -> None:
    assert "dashboard" in _ui_context_ids()


def test_manifest_entry_points_at_the_implementation_module() -> None:
    """[plugin].entry 必须指向实现模块，而不是惰性转发的根 __init__.py。

    根 __init__.py 用 __getattr__ 转发，宿主按 manifest 加载；测试按文件路径
    静态检查实现模块。两边写同一个类名才不会漂移。
    """
    manifest = _load_manifest()
    module_name, _, class_name = manifest["plugin"]["entry"].partition(":")
    assert module_name == "plugin.plugins.fnos_cli.plugin", module_name
    assert class_name == "FnOsCliPlugin", class_name
    assert (PLUGIN_DIR / "plugin.py").is_file()


def test_root_module_is_a_lazy_facade() -> None:
    """根 __init__.py 不得在顶层 import 宿主 SDK。

    pytest 收集 tests/ 时会把插件根目录当 package 导入；顶层 import
    `plugin.sdk.plugin` 在没有宿主的独立环境里会直接 ModuleNotFoundError，
    整个测试套件连收集都过不去。惰性转发是市场的既有做法。
    """
    tree = ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node.col_offset != 0:
                continue
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module is None and isinstance(node, ast.Import):
                module = node.names[0].name
            assert not str(module).startswith("plugin."), (
                f"root __init__.py must not import the host SDK at top level: {module}"
            )


def test_lifecycle_handlers_are_declared() -> None:
    """startup / config_change / shutdown 三个生命周期都要有。

    市场的 release check 会警告缺 shutdown；宿主卸载或禁用插件时调用它。
    """
    found: set[str] = set()
    for node in ast.walk(_parse_entry_module()):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if getattr(decorator.func, "attr", getattr(decorator.func, "id", "")) != "lifecycle":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    found.add(keyword.value.value)
    assert {"startup", "config_change", "shutdown"} <= found, sorted(found)


def test_run_entry_declares_command_schema() -> None:
    source = ENTRY_MODULE.read_text(encoding="utf-8")
    assert '"command"' in source, "run entry should document a command field"


# ── 命令分类（读取模块级常量，不执行模块）──────────────────────────────────


def test_write_subcommands_cover_high_risk_commands() -> None:
    writes = _module_dict("_WRITE_SUBCOMMANDS")
    assert writes, "could not read _WRITE_SUBCOMMANDS"
    for group, subcommands in writes.items():
        assert isinstance(subcommands, set) or isinstance(subcommands, (list, tuple, frozenset))
        assert subcommands, f"{group} declares no write subcommands"
    # Skill 文档中所有标注 --yes 的命令族都必须覆盖。
    for group in {"file", "storage", "docker", "download", "app", "network",
                  "logger", "monitor", "baidu-netdisk", "power", "user"}:
        assert group in writes, f"{group} missing from _WRITE_SUBCOMMANDS"


def test_blocked_subcommands_cover_media_and_login() -> None:
    blocked = _module_dict("_BLOCKED_SUBCOMMANDS")
    assert "media" in blocked
    assert "login" in blocked


def test_login_gate_lets_the_authorization_entries_through() -> None:
    """login_begin / login_finish 构造的两种 login 形状必须能过门禁。

    回归点：`_BLOCKED_SUBCOMMANDS["login"] = {"*"}` 曾把整组 login 都拦下，
    两个授权入口自己构造的 `login --authorize-only` / `login --code <code>`
    被自己的门禁挡住，从上线起就返回
    "`login` 命令不在本插件开放范围内"，授权流程完全跑不通。
    """
    source = ENTRY_MODULE.read_text(encoding="utf-8")

    # 两个入口构造的命令形状。
    assert '["login", "--authorize-only"]' in source
    assert '["login", "--code", clean_code]' in source

    # 门禁必须为 login 组开形状化的口子，而不是继续整组拦死。
    assert "_is_login_authorize_shape" in source
    assert "_is_login_code_shape" in source
    assert "_LOGIN_ALLOWED_FLAGS" in source

    # 形状判定必须是全等比较：`login`、`login --refresh`、缺值的
    # `login --code` 都不能被放行。
    assert 'tokens == ["login", "--authorize-only"]' in source
    assert "len(tokens) != 3" in source


def test_login_gate_still_blocks_interactive_login() -> None:
    """交互式 login 与刷新授权仍必须拦住。

    Skill reference/oauth.md：普通 login 会在 CLI 提示里输入 code，
    阻塞等待 TTY，插件里无法完成；`--refresh`、`--no-open` 也不在两段式
    授权流程内。放开 shapes 的同时不能把这些一起放进来。

    只扫字符串常量与可执行代码：注释里点名这些 flag 是在说明门禁设计，
    不算引用。
    """
    source = ENTRY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for forbidden in ("--refresh", "--no-open"):
        assert forbidden not in literals, f"插件不得把 {forbidden} 写进命令参数"
    # 拦下时的提示要指回专用入口，而不是只说"不在范围内"。
    assert any("login_begin / login_finish" in value for value in literals)


def test_execute_helper_returns_plain_payload_not_ok_wrapper() -> None:
    """`_execute` 必须返回 `(dict, error)`，不能把 payload 包进 Ok()。

    回归点：`_execute` 曾 `return Ok(payload), None`，调用方解包后拿到的是
    SDK 的 Result 对象而不是 dict，`isinstance(payload, dict)` 永远为 False，
    于是 login_begin / login_finish / run 全部返回"命令没有返回结果"。
    SDK 的 Ok/Err 是单一对象（moneta_fx stub 同样不可解包成元组），
    只有插件入口层才该用它们包装返回值。
    """
    source = ENTRY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 定位 _execute 的整个函数体。
    body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute":
            body = node.body
            break
    assert body is not None, "_execute not found"

    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        # 每个 return 要么是 (dict, None)，要么是 (None, SdkError)；
        # 不允许出现 Ok(...) 作为返回值的第一个元素。
        if isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2:
            first = node.value.elts[0]
            assert not (isinstance(first, ast.Call)
                        and getattr(first.func, "id", None) == "Ok"), (
                f"_execute 第 {node.lineno} 行把 payload 包进了 Ok()"
            )


def test_login_entries_echo_connection_parameters() -> None:
    """两段式授权要求两次调用使用完全相同的连接参数。

    Skill reference/oauth.md：login_begin 与 login_finish 必须保持相同
    host/port/scheme/TLS 选项，否则 pending verifier 对不上、code 兑换失败。
    因此 _execute 要把生效参数回显出来，两个入口都要转发给调用方。
    """
    source = ENTRY_MODULE.read_text(encoding="utf-8")
    assert '"connection"' in source
    assert source.count('"connection": payload.get("connection")') == 2
    # login_begin 的 llm_result_fields 要让 Agent 看到这组参数。
    assert '"authorization_pending", "connection"' in source


def test_read_only_quick_list_is_consistent() -> None:
    quick = _module_dict("_READ_ONLY_QUICK")
    assert quick
    groups = {group for group, _subs in quick}
    assert {"system", "storage", "monitor", "docker", "download"} <= groups
    for group, subcommands in quick:
        assert group
        assert subcommands, f"{group} declares no read-only subcommands"


# ── 安全约束 ──────────────────────────────────────────────────────────────


def test_panel_renders_the_authorization_link() -> None:
    """授权链接必须渲染在面板里，不能只靠 window.open 弹窗。

    回归点：beginLogin 原来只 window.open(url, "_blank")，链接本身不出现在
    页面上。弹窗被浏览器拦截、或用户想把链接复制到手机/另一台设备打开时，
    链接就彻底看不见，授权流程断死。
    """
    source = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    # pendingLogin 状态承载待处理的授权会话。
    assert "useState<PendingLogin" in source
    assert "setPendingLogin({" in source
    # 链接原文必须出现在 JSX 里（可选中、可复制）。
    assert "{pendingLogin.url}" in source
    # 弹窗降级为可点击动作，而不是唯一出口。
    assert "window.open(pendingLogin.url" in source
    # 复制按钮。
    assert "navigator.clipboard.writeText(pendingLogin.url)" in source


def test_panel_reuses_begin_connection_for_finish() -> None:
    """finishLogin 必须沿用 beginLogin 那次生效的连接参数。

    Skill reference/oauth.md：两段式授权两次调用必须使用相同
    host/port/scheme/profile，否则 pending verifier 对不上、code 兑换失败。
    用户在中途改动表单不应影响正在进行的授权。
    """
    source = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert "const conn = pendingLogin || {" in source
    # 调用必须走 conn，而不是重新读 loginForm。
    assert "host: conn.host" in source
    assert "port: conn.port" in source
    assert "scheme: conn.scheme" in source
    assert "profile: conn.profile" in source


def test_panel_collects_code_via_input_not_prompt() -> None:
    """code 用面板输入框收集，不用 window.prompt。

    prompt 在部分宿主里会被拦截或样式错乱，且用户无法确认粘贴到了什么。
    """
    source = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert "window.prompt" not in source
    assert "codeForm" in source
    assert "t(\"panel.fields.code\")" in source


def test_panel_uses_only_sdk_supported_props() -> None:
    """面板不得使用 @neko/plugin-ui 类型定义里不存在的 props。

    回归点：曾给 Alert 传 title、给 Text 传 variant="code"/selectable、
    给 Stack 传 direction="row"。SDK 里 Alert 只有 tone/message，
    Text 只有 CommonProps，Stack 只有 gap；横向排布要用 Inline。
    """
    source = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    for forbidden in (
        "<Alert",  # 单独断言，下面逐项检查
    ):
        assert forbidden in source  # Alert 仍在使用，但只准带 tone/message

    # Alert 只允许 tone 与 message。
    for match in re.finditer(r"<Alert\b([^>]*)>", source):
        props = match.group(1)
        for prop in re.findall(r"(\w+)=", props):
            assert prop in {"tone", "message"}, f"Alert 使用了不存在的 prop: {prop}"

    assert 'variant="code"' not in source
    assert "selectable" not in source
    assert 'direction="row"' not in source
    # 横向排布统一走 Inline。
    if "Inline" in source:
        assert re.search(r"<Inline\b[^>]*\bgap=", source), "Inline 需要显式 gap"


def test_all_translations_are_normalized_to_str() -> None:
    """所有 tr() 必须经 _t/_fmt 归一化，不得裸调 SDK 的 tr。

    回归点：宿主上报 `'dict' object has no attribute 'format'` —— SDK 的 tr()
    返回值不保证是 str（观察到的形态是 {key, value, locale} 记录）。插件里有
    75 处 tr() 的结果会进装饰器参数或返回给前端，任何一处漏掉都会让面板显示
    原始 dict 或直接抛异常。
    """
    source = ENTRY_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # SDK 的 tr 必须以改名形式导入，且只在 _t 内部调用一次。
    imports = [
        alias for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    sdk_tr = [a for a in imports if a.name == "tr"]
    assert sdk_tr, "plugin.py 必须从 SDK 导入 tr"
    assert sdk_tr[0].asname == "_sdk_tr", "tr 必须以 asname 导入，避免被裸用"

    sdk_tr_calls = 0
    bare_tr_calls: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == "_sdk_tr":
            sdk_tr_calls += 1
        elif name == "tr":
            bare_tr_calls.append(node.lineno)
    assert sdk_tr_calls == 1, f"_sdk_tr 应只在 _t 内调用一次，实际 {sdk_tr_calls} 次"
    assert not bare_tr_calls, f"存在裸 tr() 调用: {bare_tr_calls}"

    # _t 与 _fmt 必须存在，且 _t 做 isinstance(str) 归一化。
    assert "def _t(" in source
    assert "def _fmt(" in source
    assert "isinstance(translated, str)" in source

    # 裸 .format() 只允许出现在 _fmt 内部。
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            assert ast.unparse(node.func.value) == "translated", (
                f"第 {node.lineno} 行有裸 .format()，应改用 _fmt"
            )


def test_launcher_scripts_are_not_modified() -> None:
    """启动器与清单必须与 Skill 包一致：启动器内部有硬编码 SHA-256 校验。"""
    source_dir = PLUGIN_DIR.parent / "fnos" / "scripts"
    if not source_dir.is_dir():
        pytest.skip("original Skill package not available for comparison")
    for name in ("fnos-cli", "fnos-cli.cmd", "fnos-cli.ps1"):
        original = (source_dir / name).read_bytes()
        packaged = (PLUGIN_DIR / "scripts" / name).read_bytes()
        assert original == packaged, f"scripts/{name} must ship unmodified"

    original_manifest = (PLUGIN_DIR.parent / "fnos" / "runtime-manifest.json").read_bytes()
    packaged_manifest = (PLUGIN_DIR / "runtime-manifest.json").read_bytes()
    assert original_manifest == packaged_manifest


def test_plugin_never_logs_or_echoes_tokens() -> None:
    """插件不得读取、记录或回显 OAuth 凭据。

    只扫描可执行代码：docstring 与注释里出现这些词是在描述安全设计
    （例如"PKCE verifier 由 CLI 自行保管"），不算违规。
    """
    tree = _parse_entry_module()
    executable: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # docstring 与普通字符串常量
            continue
        if isinstance(node, ast.Name):
            executable.append(node.id)
        elif isinstance(node, ast.Attribute):
            executable.append(node.attr)

    for forbidden in ("access_token", "refresh_token", "verifier", "oauthDeviceId"):
        assert forbidden not in executable, f"plugin code references {forbidden}"
