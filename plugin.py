"""fnOS 管理插件 (fnos_cli)

把 fnos-cli Skill 包装成 N.E.K.O 插件：
- 面板（Hosted TSX）查看连接状态、系统信息、存储概览、监控指标。
- 入口点执行任意只读命令，高危写命令需要显式确认。
- 两段式 OAuth 授权：`login_begin` 返回链接，用户回传 code 后 `login_finish` 完成登录。

安全边界（与 Skill 文档一致）：
- 不代填账号密码、不读取浏览器页面 code；只接收调用方主动传入的 code。
- access/refresh token 与 PKCE verifier 由 CLI 自行保管，本插件不读取、不落盘、不回显。
- 高危写操作默认要求 `confirmed=true`，否则返回 `status=clarify` 与确认令牌。
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    PluginSettings,
    SdkError,
    SettingsField,
    lifecycle,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)

from ._cli import (
    CLI_VERSION,
    CliExecutionError,
    CliUnavailableError,
    detect_platform,
    normalize_arch,
    run_cli,
)

_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_PATH_RE = re.compile(r"^/[^\x00-\x1f]*$")

# 需要显式确认的高危子命令（对齐 Skill 文档中标注 --yes 的命令）。
_WRITE_SUBCOMMANDS = {
    "file": {"upload", "check-upload", "mkdir", "rm", "rename", "mv", "cp",
             "fav", "share", "trash"},
    "storage": {"disk-mount", "disk-umount", "mount", "umount", "create", "stop",
                "add-disk", "remove-disk", "replace-disk", "extend", "resize",
                "format", "eject"},
    "docker": {"image", "container", "compose", "network", "volume"},
    "download": {"add-uri", "add-path", "pause", "resume", "retry", "rm"},
    "app": {"install", "install-fpk", "update", "start", "stop", "uninstall",
            "openapi", "config"},
    "network": {"ssh"},
    "logger": {"clear", "export", "archive"},
    "diagnostic-log": {"export", "stop"},
    "monitor": {"mute-beeper", "request"},
    "baidu-netdisk": {"mkdir", "rm", "rename", "move", "copy", "upload", "download",
                      "share-save", "task", "sync"},
    "power": {"reboot", "poweroff"},
    "user": {"add", "mod", "del", "change-password", "unfreeze", "set-admin",
             "group", "request"},
    "login": {"logout"},
    "system": {"request"},
}

# 完全不开放给插件的命令（Skill 明确不适用或需要交互 TTY）。
# login 组只放行 login_begin / login_finish 自己构造的两个 flag；
# 交互式 login（在 CLI 提示里输入 code）会阻塞等待 TTY，必须拦住。
_LOGIN_ALLOWED_FLAGS = {"--authorize-only", "--code"}
_BLOCKED_SUBCOMMANDS = {
    "media": {"*"},
    "login": {"*"},  # 登录只走专用入口
}

_READ_ONLY_QUICK = (
    ("system", ["info", "trim-version", "kernel-version", "host-name", "machine-id",
                "uptime", "all-vols", "vols", "hardware", "is-trim-machine",
                "trim-feature", "fan-mode", "unix-time"]),
    ("storage", ["overview", "pools", "disks", "free-disks", "space", "removable"]),
    ("monitor", ["cpu", "memory", "net", "disk", "proc-list", "proc-srv",
                 "sys-warn", "battery", "sys-fan", "beep-supported"]),
    ("docker", ["stats", "image", "container", "compose"]),
    ("download", ["ls", "stat"]),
    ("app", ["list", "status", "entry", "desktop", "sac"]),
    ("file", ["ls", "search", "search-others", "usage", "is-mount-point",
              "prop", "size", "download-url", "acl", "share", "trash"]),
    ("photos", ["folders", "search", "info", "preview"]),
    ("logger", ["list", "modules", "archive"]),
    ("network", ["ssh"]),
    ("user", ["info", "list", "group", "login-devices"]),
    ("baidu-netdisk", ["status", "quota", "ls", "search", "info", "task", "sync"]),
)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(result, maximum))


@neko_plugin
class FnOsCliPlugin(NekoPluginBase):
    """fnOS 管理插件 — 连接、授权、命令执行与面板数据。"""

    class Settings(PluginSettings):
        model_config = {"toml_section": "fnos_cli"}

        host: str = SettingsField(
            "", hot=True,
            description="默认 fnOS 主机地址；留空需在调用时显式指定 / 默认为空需显式指定",
        )
        port: int = SettingsField(
            5666, hot=True, ge=1, le=65535,
            description="默认端口；回环默认 5666 / 回环默认 5666",
        )
        scheme: str = SettingsField(
            "auto", hot=True,
            description="连接协议：auto/http/https / 连接协议：auto/http/https",
        )
        profile: str = SettingsField(
            "home", hot=True,
            description="CLI profile 名（登录状态隔离）/ CLI profile 名（登录状态隔离）",
        )
        require_confirmation: bool = SettingsField(
            True, description="高危写操作是否需要显式确认 / 高危写操作是否需显式确认",
        )
        timeout_seconds: int = SettingsField(
            300, ge=5, le=3600,
            description="单条 CLI 命令最长执行秒数 / 单条 CLI 命令最长执行秒数",
        )
        return_raw_output: bool = SettingsField(
            False,
            description="是否把 CLI 原始 stdout/stderr 一并返回 / 是否返回 CLI 原始输出",
        )
        working_dir: str = SettingsField(
            "", description="CLI 工作目录；留空使用插件运行目录 / CLI 工作目录；留空用插件运行目录",
        )

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self._cfg: Dict[str, Any] = {}
        self._last_result: Optional[Dict[str, Any]] = None
        self._launcher_error: Optional[str] = None

    # ── 生命周期 ──

    @lifecycle(id="startup")
    async def startup(self, **_):
        await self._reload_config()
        platform_info = await detect_platform(self.plugin_dir)
        self._launcher_error = platform_info.get("launcher_error")
        if platform_info.get("launcher_ok"):
            self.logger.info(
                "fnos_cli ready: launcher={}, os={}, arch={}, version={}",
                platform_info.get("launcher"),
                platform_info.get("os"),
                normalize_arch(platform_info.get("arch", "")),
                CLI_VERSION,
            )
        else:
            self.logger.warning(
                "fnos_cli launcher unavailable: {}", self._launcher_error or "unknown"
            )
        return Ok({"status": "ready", "platform": platform_info})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        # 插件不持有长连接或子进程：每次命令都是短生命周期的子进程，
        # 执行完毕后自行退出。这里只丢弃进程内缓存，不触碰 CLI 的登录态。
        self._last_result = None
        self._launcher_error = None
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._reload_config()
        return Ok({"status": "reloaded"})

    async def _reload_config(self) -> None:
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("fnos_cli")
        self._cfg = dict(section) if isinstance(section, dict) else {}

    # ── 内部辅助 ──

    def _setting(self, key: str, default: Any) -> Any:
        value = self._cfg.get(key, default)
        return default if value is None else value

    def _timeout(self) -> float:
        return float(_clamp_int(self._setting("timeout_seconds", 300), 300, 5, 3600))

    def _work_dir(self) -> Optional[Path]:
        raw = _as_text(self._setting("working_dir", ""))
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if path.is_dir() else None

    @staticmethod
    def _validate_profile(profile: str) -> str:
        # fullmatch：`$` 会容忍末尾换行，fullmatch 不会。
        profile = _as_text(profile)
        if profile and not _PROFILE_RE.fullmatch(profile):
            raise ValueError(
                "profile 只能包含 ASCII 字母、数字、_ - . （当前值不合法）"
            )
        return profile

    @staticmethod
    def _validate_host(host: str) -> str:
        host = _as_text(host)
        if host and not _HOST_RE.fullmatch(host):
            raise ValueError("host 含非法字符（仅允许字母、数字、. 和 -）")
        return host

    @staticmethod
    def _split_command(command: str) -> List[str]:
        """把命令字符串按 shell 词法切分，不做任何求值。"""
        text = _as_text(command)
        if not text:
            return []
        try:
            return shlex.split(text, posix=True)
        except ValueError as exc:
            raise ValueError(f"命令引号不配对: {exc}") from exc

    def _requires_confirmation(self, tokens: List[str]) -> bool:
        """判断一条命令是否属于高危写操作。"""
        if not tokens:
            return False
        group = tokens[0]
        # 两段式授权放行的两种 login 形状本来就走 confirmed=True，
        # 且 Skill 的 login 命令不接受 --yes，不能让它们进入写操作分支。
        if group == "login":
            if self._is_login_authorize_shape(tokens) or self._is_login_code_shape(tokens):
                return False
        if group in _BLOCKED_SUBCOMMANDS:
            blocked = _BLOCKED_SUBCOMMANDS[group]
            if "*" in blocked:
                return True
        writes = _WRITE_SUBCOMMANDS.get(group)
        if not writes:
            return False
        if len(tokens) < 2:
            return True
        return tokens[1] in writes

    def _check_blocked(self, tokens: List[str]) -> Optional[str]:
        if not tokens:
            return "命令为空"
        group = tokens[0]
        blocked = _BLOCKED_SUBCOMMANDS.get(group)
        if blocked:
            # login 组只放行 login_begin / login_finish 自己构造的两种形状：
            #   login --authorize-only
            #   login --code <value>
            # 交互式 login（阻塞等 TTY）、--refresh、--no-open 以及任何多出来的
            # flag 或缺值的 --code 一律拦下。
            if group == "login":
                if self._is_login_authorize_shape(tokens) or self._is_login_code_shape(tokens):
                    return None
                return "`login` 命令不在本插件开放范围内；请使用 login_begin / login_finish 入口"
            if "*" in blocked:
                return f"`{group}` 命令不在本插件开放范围内"
            for token in tokens[1:]:
                if token in blocked:
                    return f"`{group} {token}` 不在本插件开放范围内"
        return None

    @staticmethod
    def _is_login_authorize_shape(tokens: List[str]) -> bool:
        """恰好 `login --authorize-only`。"""
        return tokens == ["login", "--authorize-only"]

    @staticmethod
    def _is_login_code_shape(tokens: List[str]) -> bool:
        """恰好 `login --code <value>`，value 非空且不是另一个 flag。"""
        if len(tokens) != 3:
            return False
        flag, value = tokens[1], tokens[2]
        return flag == "--code" and bool(value) and not value.startswith("-")

    async def _execute(
        self,
        tokens: List[str],
        *,
        confirmed: bool = False,
        host: str = "",
        port: Optional[int] = None,
        scheme: str = "",
        profile: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], Optional[SdkError]]:
        """执行命令；需要确认而未确认时返回 clarify 结果。"""
        blocked = self._check_blocked(tokens)
        if blocked:
            return None, SdkError(blocked)

        needs_confirm = self._requires_confirmation(tokens)
        require = bool(self._setting("require_confirmation", True))
        if needs_confirm and require and not confirmed:
            rendered = " ".join(shlex.quote(t) for t in tokens)
            return {
                "status": "clarify",
                "summary": tr(
                    "fnos.confirm_write",
                    default="这是写操作，会影响 fnOS 设备状态：{command}。确认执行吗？",
                ).format(command=rendered),
                "choices": [
                    tr("fnos.confirm_yes", default="确认执行"),
                    tr("fnos.confirm_no", default="取消"),
                ],
                "context": {
                    "command": rendered,
                    "confirmed": True,
                },
            }, None

        effective_host = _as_text(host) or _as_text(self._setting("host", ""))
        effective_port = int(port) if port else int(_clamp_int(self._setting("port", 5666), 5666, 1, 65535))
        effective_scheme = _as_text(scheme) or _as_text(self._setting("scheme", "auto")) or "auto"
        effective_profile = self._validate_profile(
            _as_text(profile) or _as_text(self._setting("profile", "home"))
        )

        argv = list(tokens)
        if needs_confirm and "--yes" not in argv:
            argv.append("--yes")

        try:
            result = await run_cli(
                plugin_dir=self.plugin_dir,
                args=argv,
                host=effective_host,
                port=effective_port,
                scheme=effective_scheme,
                profile=effective_profile,
                timeout_seconds=self._timeout(),
                cwd=self._work_dir(),
            )
        except CliUnavailableError as exc:
            return None, SdkError(f"fnos-cli 启动器不可用: {exc}")
        except CliExecutionError as exc:
            payload: Dict[str, Any] = {
                "status": "error",
                "summary": str(exc),
                "timed_out": exc.timed_out,
            }
            if bool(self._setting("return_raw_output", False)):
                payload["stdout"] = exc.stdout
                payload["stderr"] = exc.stderr
            self._last_result = payload
            return payload, None
        except ValueError as exc:
            return None, SdkError(str(exc))

        payload = {
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            # 回显生效的连接参数：两段式授权要求 login_begin 与 login_finish
            # 使用完全相同的 host/port/scheme/profile，否则 pending verifier
            # 对不上、code 兑换失败（Skill reference/oauth.md）。
            "connection": {
                "host": effective_host,
                "port": effective_port,
                "scheme": effective_scheme,
                "profile": effective_profile,
            },
        }
        if bool(self._setting("return_raw_output", False)):
            payload["stdout"] = result.stdout
            payload["stderr"] = result.stderr
        else:
            payload["stdout_tail"] = result.stdout[-4000:]
            payload["stderr_tail"] = result.stderr[-2000:]
        self._last_result = payload
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            payload["summary"] = (
                tr("fnos.command_failed", default="命令执行失败（退出码 {code}）：{detail}")
                .format(code=result.returncode, detail=detail[-1] if detail else "无错误输出")
            )
        else:
            payload["summary"] = tr("fnos.command_ok", default="命令执行成功")
        return payload, None

    # ── 面板数据 ──

    @ui.context(id="dashboard", title=tr("panel.title", default="fnOS 管理"))
    async def dashboard_context(self) -> Dict[str, Any]:
        platform_info = await detect_platform(self.plugin_dir)
        return {
            "config": {
                "host": _as_text(self._setting("host", "")),
                "port": int(_clamp_int(self._setting("port", 5666), 5666, 1, 65535)),
                "scheme": _as_text(self._setting("scheme", "auto")) or "auto",
                "profile": _as_text(self._setting("profile", "home")),
                "require_confirmation": bool(self._setting("require_confirmation", True)),
                "timeout_seconds": int(_clamp_int(self._setting("timeout_seconds", 300), 300, 5, 3600)),
                "working_dir": _as_text(self._setting("working_dir", "")),
            },
            "platform": {
                "os": platform_info.get("os"),
                "arch": normalize_arch(platform_info.get("arch", "")),
                "launcher_ok": bool(platform_info.get("launcher_ok")),
                "launcher": platform_info.get("launcher"),
                "launcher_error": platform_info.get("launcher_error"),
                "cli_version": CLI_VERSION,
            },
            "launcher_ok": bool(platform_info.get("launcher_ok")),
            "last_result": self._last_result,
            "quick_commands": [
                {"group": group, "subcommands": list(subs)}
                for group, subs in _READ_ONLY_QUICK
            ],
        }

    # ── 连接与授权入口 ──

    @ui.action(
        label=tr("actions.status.label", default="连接状态"),
        icon="📡",
        group="connection",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="status",
        name=tr("entries.status.name", default="连接状态"),
        description=tr(
            "entries.status.description",
            default="查看启动器可用性、平台信息与当前默认连接参数。不连接设备。",
        ),
        llm_result_fields=["summary", "launcher_ok", "config"],
    )
    async def status(self, **_):
        platform_info = await detect_platform(self.plugin_dir)
        host = _as_text(self._setting("host", ""))
        summary = (
            tr(
                "fnos.status_ready",
                default="启动器可用（{os}/{arch}），默认目标 {target}，profile {profile}。",
            ).format(
                os=platform_info.get("os"),
                arch=normalize_arch(platform_info.get("arch", "")),
                target=f"{host or 'localhost'}:{int(_clamp_int(self._setting('port', 5666), 5666, 1, 65535))}",
                profile=_as_text(self._setting("profile", "home")),
            )
            if platform_info.get("launcher_ok")
            else tr("fnos.status_no_launcher", default="启动器不可用：{error}")
            .format(error=platform_info.get("launcher_error") or "unknown")
        )
        return Ok({
            "status": "ready" if platform_info.get("launcher_ok") else "unavailable",
            "summary": summary,
            "launcher_ok": bool(platform_info.get("launcher_ok")),
            "platform": platform_info,
            "config": {
                "host": host,
                "port": int(_clamp_int(self._setting("port", 5666), 5666, 1, 65535)),
                "scheme": _as_text(self._setting("scheme", "auto")) or "auto",
                "profile": _as_text(self._setting("profile", "home")),
            },
        })

    @ui.action(
        label=tr("actions.loginBegin.label", default="开始授权"),
        icon="🔑",
        group="connection",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="login_begin",
        name=tr("entries.loginBegin.name", default="开始 OAuth 授权"),
        description=tr(
            "entries.loginBegin.description",
            default="生成 fnOS 授权链接。把链接交给用户在浏览器完成登录，再让用户回传一次性 code，然后调用 login_finish。login_finish 必须沿用本次返回的 connection 参数，否则 code 兑换失败。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": tr("fields.host", default="fnOS 主机地址")},
                "port": {"type": "integer", "description": tr("fields.port", default="端口")},
                "scheme": {"type": "string", "enum": ["auto", "http", "https"], "description": tr("fields.scheme", default="协议")},
                "profile": {"type": "string", "description": tr("fields.profile", default="CLI profile 名")},
            },
        },
        llm_result_fields=["summary", "authorization_url", "authorization_pending", "connection"],
    )
    async def login_begin(
        self,
        host: str = "",
        port: Optional[int] = None,
        scheme: str = "",
        profile: str = "",
        **_,
    ):
        try:
            effective_host = self._validate_host(host)
            effective_profile = self._validate_profile(profile)
        except ValueError as exc:
            return Err(SdkError(str(exc)))

        tokens = ["login", "--authorize-only"]
        payload, error = await self._execute(
            tokens,
            confirmed=True,
            host=effective_host,
            port=port,
            scheme=scheme,
            profile=effective_profile,
        )
        if error is not None:
            return Err(error)
        if not isinstance(payload, dict):
            return Err(SdkError(tr("fnos.unexpected", default="命令没有返回结果")))

        stdout = payload.get("stdout") or payload.get("stdout_tail") or ""
        url = _extract_url(stdout)
        if not url:
            return Err(SdkError(
                tr("fnos.login_no_url", default="未能在 CLI 输出中找到授权链接；请检查 host/port 与设备连通性。")
            ))
        pending = "authorization_pending" in stdout or '"authorization_pending": true' in stdout
        return Ok({
            "status": "ready",
            "summary": tr(
                "fnos.login_begin_summary",
                default="请在浏览器打开授权链接并登录，然后把页面显示的一次性 code 发回给我。",
            ),
            "authorization_url": url,
            "authorization_pending": bool(pending),
            # 调用方回传 code 时必须沿用这组参数，否则 code 兑换失败。
            "connection": payload.get("connection"),
        })

    @ui.action(
        label=tr("actions.loginFinish.label", default="完成授权"),
        icon="✅",
        group="connection",
        order=30,
        refresh_context=True,
    )
    @plugin_entry(
        id="login_finish",
        name=tr("entries.loginFinish.name", default="完成 OAuth 授权"),
        description=tr(
            "entries.loginFinish.description",
            default="使用用户回传的一次性 code 完成登录。必须与 login_begin 使用同一执行主机、系统用户、profile 和连接参数。code 只在本次生效，不要复用。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": tr("fields.code", default="用户回传的一次性授权 code")},
                "host": {"type": "string", "description": tr("fields.host", default="fnOS 主机地址")},
                "port": {"type": "integer", "description": tr("fields.port", default="端口")},
                "scheme": {"type": "string", "enum": ["auto", "http", "https"], "description": tr("fields.scheme", default="协议")},
                "profile": {"type": "string", "description": tr("fields.profile", default="CLI profile 名")},
            },
            "required": ["code"],
        },
        llm_result_fields=["summary", "token_persisted", "connection"],
    )
    async def login_finish(
        self,
        code: str = "",
        host: str = "",
        port: Optional[int] = None,
        scheme: str = "",
        profile: str = "",
        **_,
    ):
        clean_code = _as_text(code)
        if not clean_code:
            return Err(SdkError(tr("fnos.code_required", default="缺少用户回传的授权 code。")))
        if "\n" in clean_code or "\r" in clean_code or len(clean_code) > 512:
            return Err(SdkError(tr("fnos.code_invalid", default="code 格式不合法。")))
        try:
            effective_host = self._validate_host(host)
            effective_profile = self._validate_profile(profile)
        except ValueError as exc:
            return Err(SdkError(str(exc)))

        tokens = ["login", "--code", clean_code]
        payload, error = await self._execute(
            tokens,
            confirmed=True,
            host=effective_host,
            port=port,
            scheme=scheme,
            profile=effective_profile,
        )
        if error is not None:
            return Err(error)
        if not isinstance(payload, dict):
            return Err(SdkError(tr("fnos.unexpected", default="命令没有返回结果")))
        if payload.get("returncode") != 0:
            return Err(SdkError(
                tr("fnos.login_failed", default="登录失败：{detail}")
                .format(detail=payload.get("summary") or "未知错误")
            ))
        stdout = payload.get("stdout") or payload.get("stdout_tail") or ""
        persisted = '"token_persisted": true' in stdout or "token_persisted=true" in stdout
        return Ok({
            "status": "ready",
            "summary": tr(
                "fnos.login_done",
                default="登录已完成，令牌已保存在 CLI 受控存储中。可以用 system info 做只读验证。",
            ),
            "token_persisted": bool(persisted),
            "connection": payload.get("connection"),
        })

    @ui.action(
        label=tr("actions.logout.label", default="退出登录"),
        icon="🚪",
        group="connection",
        order=40,
        confirm=True,
        refresh_context=True,
    )
    @plugin_entry(
        id="logout",
        name=tr("entries.logout.name", default="退出登录"),
        description=tr(
            "entries.logout.description",
            default="清除当前 profile 的本机登录会话。不影响服务端令牌撤销。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": tr("fields.profile", default="CLI profile 名")},
                "confirmed": {"type": "boolean", "default": False, "description": tr("fields.confirmed", default="显式确认")},
            },
        },
    )
    async def logout(self, profile: str = "", confirmed: bool = False, **_):
        try:
            effective_profile = self._validate_profile(profile)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        if bool(self._setting("require_confirmation", True)) and not confirmed:
            return Ok({
                "status": "clarify",
                "summary": tr("fnos.confirm_logout", default="确认清除 profile {profile} 的本机登录会话？")
                .format(profile=effective_profile or _as_text(self._setting("profile", "home"))),
                "choices": [tr("fnos.confirm_yes", default="确认执行"), tr("fnos.confirm_no", default="取消")],
                "context": {"profile": effective_profile, "confirmed": True},
            })
        payload, error = await self._execute(
            ["logout"], confirmed=True, profile=effective_profile,
        )
        if error is not None:
            return Err(error)
        return Ok({
            "status": "ready",
            "summary": tr("fnos.logout_done", default="已清除本机登录会话。"),
        })

    # ── 通用命令入口 ──

    @ui.action(
        label=tr("actions.run.label", default="执行命令"),
        icon="▶️",
        group="command",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="run",
        name=tr("entries.run.name", default="执行 fnos-cli 命令"),
        description=tr(
            "entries.run.description",
            default=(
                "执行一条 fnos-cli 命令，例如 `system info`、`storage overview`、"
                "`monitor cpu`、`file ls /vol1/downloads`。写操作需要 confirmed=true。"
                "命令会按 shell 词法切分后以 argv 传递，不经过 shell 求值。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": tr(
                        "fields.command",
                        default="命令串，不含 fnos-cli 前缀，例如 `storage pools`",
                    ),
                },
                "confirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": tr("fields.confirmed", default="写操作显式确认"),
                },
                "host": {"type": "string", "description": tr("fields.host", default="fnOS 主机地址")},
                "port": {"type": "integer", "description": tr("fields.port", default="端口")},
                "scheme": {"type": "string", "enum": ["auto", "http", "https"], "description": tr("fields.scheme", default="协议")},
                "profile": {"type": "string", "description": tr("fields.profile", default="CLI profile 名")},
            },
            "required": ["command"],
        },
        timeout=600.0,
        llm_result_fields=["summary", "status", "returncode"],
    )
    async def run(
        self,
        command: str = "",
        confirmed: bool = False,
        host: str = "",
        port: Optional[int] = None,
        scheme: str = "",
        profile: str = "",
        **_,
    ):
        try:
            tokens = self._split_command(command)
            if not tokens:
                return Err(SdkError(tr("fnos.command_empty", default="命令不能为空。")))
            effective_host = self._validate_host(host)
            effective_profile = self._validate_profile(profile)
        except ValueError as exc:
            return Err(SdkError(str(exc)))

        payload, error = await self._execute(
            tokens,
            confirmed=bool(confirmed),
            host=effective_host,
            port=port,
            scheme=scheme,
            profile=effective_profile,
        )
        if error is not None:
            return Err(error)
        if not isinstance(payload, dict):
            return Err(SdkError(tr("fnos.unexpected", default="命令没有返回结果")))
        payload = dict(payload)
        payload.setdefault("summary", tr("fnos.command_ok", default="命令执行成功"))
        payload["command"] = " ".join(tokens)
        return Ok(payload)

    # ── 常用只读快捷入口 ──

    @ui.action(
        label=tr("actions.systemInfo.label", default="系统信息"),
        icon="🖥️",
        group="read",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="system_info",
        name=tr("entries.systemInfo.name", default="系统信息"),
        description=tr(
            "entries.systemInfo.description",
            default="读取 fnOS 机器类型、版本、卷等静态系统标识。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def system_info(self, host: str = "", port: Optional[int] = None,
                          scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["system", "info"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.storageOverview.label", default="存储概览"),
        icon="💾",
        group="read",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="storage_overview",
        name=tr("entries.storageOverview.name", default="存储概览"),
        description=tr(
            "entries.storageOverview.description",
            default="读取存储池、磁盘与空间总览。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def storage_overview(self, host: str = "", port: Optional[int] = None,
                               scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["storage", "overview"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.storagePools.label", default="存储池列表"),
        icon="🗄️",
        group="read",
        order=30,
        refresh_context=True,
    )
    @plugin_entry(
        id="storage_pools",
        name=tr("entries.storagePools.name", default="存储池列表"),
        description=tr(
            "entries.storagePools.description",
            default="列出存储池（uuid / trim_* 标识）。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def storage_pools(self, host: str = "", port: Optional[int] = None,
                            scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["storage", "pools"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.storageDisks.label", default="磁盘列表"),
        icon="🛠️",
        group="read",
        order=40,
        refresh_context=True,
    )
    @plugin_entry(
        id="storage_disks",
        name=tr("entries.storageDisks.name", default="磁盘列表"),
        description=tr(
            "entries.storageDisks.description",
            default="列出物理磁盘（纯设备名）。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def storage_disks(self, host: str = "", port: Optional[int] = None,
                            scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["storage", "disks"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.monitorCpu.label", default="CPU 监控"),
        icon="📈",
        group="read",
        order=50,
        refresh_context=True,
    )
    @plugin_entry(
        id="monitor_cpu",
        name=tr("entries.monitorCpu.name", default="CPU 监控"),
        description=tr(
            "entries.monitorCpu.description",
            default="读取 CPU 时间序列指标。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def monitor_cpu(self, host: str = "", port: Optional[int] = None,
                          scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["monitor", "cpu"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.monitorMemory.label", default="内存监控"),
        icon="🧠",
        group="read",
        order=60,
        refresh_context=True,
    )
    @plugin_entry(
        id="monitor_memory",
        name=tr("entries.monitorMemory.name", default="内存监控"),
        description=tr(
            "entries.monitorMemory.description",
            default="读取内存时间序列指标。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def monitor_memory(self, host: str = "", port: Optional[int] = None,
                             scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["monitor", "memory"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.downloadList.label", default="下载任务"),
        icon="⬇️",
        group="read",
        order=70,
        refresh_context=True,
    )
    @plugin_entry(
        id="download_list",
        name=tr("entries.downloadList.name", default="下载任务列表"),
        description=tr(
            "entries.downloadList.description",
            default="列出下载任务。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def download_list(self, host: str = "", port: Optional[int] = None,
                            scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["download", "ls"], host=host, port=port, scheme=scheme, profile=profile,
        )

    @ui.action(
        label=tr("actions.dockerStats.label", default="Docker 状态"),
        icon="🐳",
        group="read",
        order=80,
        refresh_context=True,
    )
    @plugin_entry(
        id="docker_stats",
        name=tr("entries.dockerStats.name", default="Docker 状态"),
        description=tr(
            "entries.dockerStats.description",
            default="读取 Docker 总体状态。只读，无需确认。",
        ),
        llm_result_fields=["summary", "status"],
    )
    async def docker_stats(self, host: str = "", port: Optional[int] = None,
                           scheme: str = "", profile: str = "", **_):
        return await self._read_shortcut(
            ["docker", "stats"], host=host, port=port, scheme=scheme, profile=profile,
        )

    async def _read_shortcut(
        self,
        tokens: List[str],
        *,
        host: str,
        port: Optional[int],
        scheme: str,
        profile: str,
    ):
        try:
            effective_host = self._validate_host(host)
            effective_profile = self._validate_profile(profile)
        except ValueError as exc:
            return Err(SdkError(str(exc)))
        payload, error = await self._execute(
            tokens,
            confirmed=True,
            host=effective_host,
            port=port,
            scheme=scheme,
            profile=effective_profile,
        )
        if error is not None:
            return Err(error)
        if not isinstance(payload, dict):
            return Err(SdkError(tr("fnos.unexpected", default="命令没有返回结果")))
        payload = dict(payload)
        payload["command"] = " ".join(tokens)
        return Ok(payload)


_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def _extract_url(text: str) -> str:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else ""
