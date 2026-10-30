# fnOS 管理插件 Design Brief

## Identity Lock

- plugin_id: `fnos_cli`
- folder: `fnos_cli/`
- name: fnOS 管理
- entry: `plugin.plugins.fnos_cli.plugin:FnOsCliPlugin`
- main class: `FnOsCliPlugin`

## Purpose

把 fnos-cli Skill 封装成 N.E.K.O 插件，使用户能在面板里连接、授权并管理 fnOS 设备，
并让 Agent 在对话中调用同一组能力。插件自身不实现 fnOS 协议，全部命令交由随包的
固定版本启动器执行。

## Package Type and Capabilities

- package type: `plugin`
- capabilities: 外部设备/CLI 集成、Hosted TSX 面板、Markdown 指南、typed settings、键值 store
- inferred architecture: 单插件类（`plugin.py`）+ 一个 CLI 执行辅助模块（`_cli.py`）；不引入 Router。根 `__init__.py` 只做 `__getattr__` 惰性转发

## First Version Scope

- `status` / `login_begin` / `login_finish` / `logout` 连接与授权
- `run` 通用命令入口（阻断清单 + 写操作确认）
- 只读快捷入口：`system_info`、`storage_overview`、`storage_pools`、`storage_disks`、
  `monitor_cpu`、`monitor_memory`、`download_list`、`docker_stats`
- Hosted TSX 面板：启动器状态、连接参数、授权两步、配置保存、只读快捷按钮、命令执行、最近结果
- `docs/quickstart.md` Markdown 指南
- zh-CN / en 两套 locale

## Out of Scope

- 不实现 fnOS HTTP 协议、签名或 OAuth 细节（由 CLI 负责）
- 不提供交互式 `login` 与 `media` 命令
- 不做设备侧文件浏览 UI，只做命令入口
- 不做定时轮询 / 主动推送（无 timer 与 message 处理器）

## Inferred Technical Needs

- plugin.toml sections: `[plugin]`、`[plugin.author]`、`[plugin.sdk]`、`[plugin.i18n]`、
  `[plugin_runtime]`、`[plugin.store]`、`[plugin.ui]`、`[fnos_cli]`
- SDK surfaces: `NekoPluginBase`、`@neko_plugin`、`@plugin_entry`、`@lifecycle(startup/config_change/shutdown)`、
  `@ui.context`、`@ui.action`、`tr()`、`PluginSettings`/`SettingsField`、`Ok`/`Err`/`SdkError`
- UI surfaces: 一个 `panel`（hosted-tsx）+ 一个 `guide`（markdown），permissions 取最小集
  `state:read` / `config:read` / `action:call`
- state/config: 配置走 `PluginSettings`（`[fnos_cli]` section）；`last_result` 仅存进程内最近一次执行摘要，不落 store
- lifecycle/background work: 仅 startup 检测启动器与平台，config_change 重载配置，shutdown 丢弃进程内缓存
- external integrations: 子进程调用随包启动器；`TRIM_CLI_DISTRIBUTION=fnos` 固定分发标识

## Read Context Plan

- Skill 包：`SKILL.md`、`entries/trim-*.md`、`reference/*.md`（确定命令面与写操作清单）
- Skill 包：`scripts/fnos-cli*`、`runtime-manifest.json`（启动器行为与校验绑定）
- N.E.K.O：插件文档 plugin-development / quick-start / plugin-toml / hosted-ui / sdk-reference /
  decorators / best-practices，以及 lifekit 插件的 `plugin.toml`、`ui/panel.tsx`、`__init__.py` 作为参照实现

## Write Workspace

`fnos_cli/`（整个插件目录）

## Risk Follow-ups

- 凭据边界：code 只在调用方显式传入时使用，插件不落盘、不记日志、不回显；token 由 CLI 保管。已按此实现。
- 写操作风险：默认 `require_confirmation=true`，未确认返回 `clarify` 与上下文；`_WRITE_SUBCOMMANDS`
  覆盖 Skill 文档中所有标注 `--yes` 的命令。
- 启动器完整性：`runtime-manifest.json` 与三个启动器脚本原样拷贝，未做任何修改，避免破坏内部 SHA-256 校验。
- 平台差异：Windows 走 `.cmd` → `.ps1`，其他平台直接执行；均通过 `asyncio.create_subprocess_exec`
  以 argv 传递，不经过 shell。
