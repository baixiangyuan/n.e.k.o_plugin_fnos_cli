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


def _load_manifest() -> dict:
    with (PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        return tomllib.load(handle)


def _parse_entry_module() -> ast.Module:
    return ast.parse((PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8"))


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


def test_run_entry_declares_command_schema() -> None:
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
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


def test_read_only_quick_list_is_consistent() -> None:
    quick = _module_dict("_READ_ONLY_QUICK")
    assert quick
    groups = {group for group, _subs in quick}
    assert {"system", "storage", "monitor", "docker", "download"} <= groups
    for group, subcommands in quick:
        assert group
        assert subcommands, f"{group} declares no read-only subcommands"


# ── 安全约束 ──────────────────────────────────────────────────────────────


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
