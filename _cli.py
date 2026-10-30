"""fnos-cli 执行核心：定位启动器、构造 argv、捕获输出。

插件只依赖 Skill 包内随附的启动器脚本（`scripts/fnos-cli*`）。启动器自己负责
下载、SHA-256 校验和解包对应平台的原生 CLI；本模块不复制这段逻辑，也不
单独调用原生二进制。

安全约束（对齐 Skill 文档的行为）：
- 不使用 shell，所有参数以 argv 列表传递，不经过字符串拼接。
- `code`、密码类输入只从调用方显式传入，本模块不记录、不回显。
- 高危命令（写操作）由上层入口决定是否要求确认。
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 启动器相对插件根目录的位置。
_LAUNCHER_RELATIVE = Path("scripts") / "fnos-cli"
_LAUNCHER_WINDOWS_RELATIVE = Path("scripts") / "fnos-cli.cmd"
_LAUNCHER_POWERSHELL_RELATIVE = Path("scripts") / "fnos-cli.ps1"

# CLI 版本（与 runtime-manifest.json 固定绑定）。
CLI_VERSION = "0.5.1"


class CliUnavailableError(RuntimeError):
    """启动器缺失或当前平台不受支持。"""


class CliExecutionError(RuntimeError):
    """CLI 执行失败（非零退出、超时或无法启动）。"""

    def __init__(self, message: str, *, returncode: Optional[int] = None,
                 stdout: str = "", stderr: str = "", timed_out: bool = False) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


@dataclass(frozen=True)
class CliResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int

    def as_dict(self, *, include_argv: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
        }
        if include_argv:
            payload["argv"] = list(self.argv)
        return payload


def is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def resolve_launcher(plugin_dir: Path) -> Path:
    """返回当前平台使用的启动器绝对路径。"""
    if is_windows():
        # .cmd 入口通过系统 PowerShell 调用 .ps1，参数按原始 argv 传递，
        # 避免 PowerShell -File 对空串和引号的重新解释。
        candidate = plugin_dir / _LAUNCHER_WINDOWS_RELATIVE
        if candidate.is_file():
            return candidate
        candidate = plugin_dir / _LAUNCHER_POWERSHELL_RELATIVE
        if candidate.is_file():
            return candidate
    else:
        candidate = plugin_dir / _LAUNCHER_RELATIVE
        if candidate.is_file():
            return candidate
        # 极少数情况（例如被拷贝到无执行位的位置）下退回 sh 显式调用。
        candidate = plugin_dir / _LAUNCHER_RELATIVE
        if candidate.is_file():
            sh = shutil.which("sh")
            if sh:
                return candidate
    raise CliUnavailableError(
        f"fnos-cli launcher not found under {plugin_dir / 'scripts'}"
    )


def _needs_shell(launcher: Path) -> bool:
    """.cmd 需要 cmd.exe 包装；.ps1 需要 powershell.exe；其他直接 exec。"""
    name = launcher.name.lower()
    if is_windows():
        if name.endswith(".cmd"):
            return True
        if name.endswith(".ps1"):
            return "powershell"
    return False


def build_invocation(launcher: Path) -> tuple[List[str], Optional[str]]:
    """返回 (argv_prefix, shell) 组合，shell 仅在 Windows 上有值。"""
    mode = _needs_shell(launcher)
    if mode is True:
        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/d", "/s", "/c", str(launcher)], None
    if mode == "powershell":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            raise CliUnavailableError("PowerShell not found; cannot run fnos-cli.ps1")
        return [
            powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(launcher),
        ], None
    return [str(launcher)], None


def _decode(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "utf-8-sig", "mbcs", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _connection_args(
    host: str,
    port: Optional[int],
    scheme: str,
    profile: str,
) -> List[str]:
    args: List[str] = []
    host = (host or "").strip()
    if host:
        args += ["--host", host]
    if port:
        args += ["--port", str(int(port))]
    scheme = (scheme or "").strip().lower()
    if scheme and scheme != "auto":
        args += ["--scheme", scheme]
    profile = (profile or "").strip()
    if profile:
        args += ["--profile", profile]
    return args


async def run_cli(
    *,
    plugin_dir: Path,
    args: Sequence[str],
    host: str = "",
    port: Optional[int] = None,
    scheme: str = "auto",
    profile: str = "",
    timeout_seconds: float = 300.0,
    env_extra: Optional[Dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> CliResult:
    """执行 fnos-cli 并捕获 stdout/stderr。不抛异常即代表进程正常结束。"""
    launcher = resolve_launcher(plugin_dir)
    prefix, _shell = build_invocation(launcher)
    argv = [*prefix, *_connection_args(host, port, scheme, profile), *[str(a) for a in args]]

    env = dict(os.environ)
    # 固定分发标识，避免启动器从环境里读到其他产品的设置。
    env["TRIM_CLI_DISTRIBUTION"] = "fnos"
    if env_extra:
        for key, value in env_extra.items():
            env[str(key)] = str(value)

    work_dir = str(cwd) if cwd and Path(cwd).is_dir() else str(plugin_dir)
    started = asyncio.get_running_loop().time()

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=env,
        )
    except FileNotFoundError as exc:
        raise CliUnavailableError(f"cannot start fnos-cli launcher: {exc}") from exc
    except OSError as exc:
        raise CliUnavailableError(f"cannot start fnos-cli launcher: {exc}") from exc

    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(
            process.communicate(), timeout=max(1.0, float(timeout_seconds))
        )
    except asyncio.TimeoutError as exc:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise CliExecutionError(
            f"fnos-cli timed out after {timeout_seconds:.0f}s",
            timed_out=True,
        ) from exc

    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
    returncode = process.returncode if process.returncode is not None else -1
    stdout = _decode(stdout_raw)
    stderr = _decode(stderr_raw)
    return CliResult(
        argv=argv,
        returncode=int(returncode),
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )


async def detect_platform(plugin_dir: Path) -> Dict[str, Any]:
    """返回宿主平台信息与启动器是否可用。"""
    info: Dict[str, Any] = {
        "os": platform.system().lower(),
        "arch": platform.machine().lower(),
        "python": platform.python_version(),
        "launcher": None,
        "launcher_ok": False,
        "cli_version": CLI_VERSION,
    }
    try:
        launcher = resolve_launcher(plugin_dir)
        info["launcher"] = str(launcher)
        info["launcher_ok"] = True
    except CliUnavailableError as exc:
        info["launcher_error"] = str(exc)
    return info


def normalize_arch(machine: str) -> str:
    machine = (machine or "").lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "x64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine or "unknown"
