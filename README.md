# fnOS 管理 (fnos_cli)

把 [fnos-cli](https://github.com/fnnas-labs/fnos-cli-skill) Skill 包装成 N.E.K.O 插件：
一个面板（Hosted TSX）+ 一组入口点，用来连接、授权并管理 fnOS（飞牛 NAS）设备。

## 能力范围

| 模块 | 只读 | 写操作（需确认） |
| --- | --- | --- |
| 系统 / 电源 | `system *` | `power reboot` / `power poweroff` |
| 存储 | `storage overview` / `pools` / `disks` / `free-disks` / `space` / `smart` / `health` | `create` / `mount` / `umount` / `stop` / `add-disk` / `remove-disk` / `replace-disk` / `extend` / `resize` / `format` / `eject` / `disk-mount` / `disk-umount` |
| 文件 | `file ls` / `search` / `prop` / `acl` / `share` / `usage` / `trash` | `upload` / `mkdir` / `rm` / `rename` / `mv` / `cp` / `fav` |
| Docker | `docker stats` / `image ls` / `container ls` / `compose ls` | `image pull` / `container` 生命周期 / `compose` 写操作 |
| 下载 | `download ls` / `info` / `files` / `stat` | `add-uri` / `add-path` / `pause` / `resume` / `retry` / `rm` |
| 应用中心 | `app list` / `status` / `entry` / `desktop` / `sac` | `install` / `install-fpk` / `update` / `start` / `stop` / `uninstall` / `openapi` |
| 日志 | `logger list` / `modules` / `archive query` | `logger clear` / `export` / `archive set`、`diagnostic-log` |
| 监控 | `monitor cpu` / `memory` / `net` / `disk` / `proc-*` / `battery` / `sys-fan` / `beep-*` | `mute-beeper` |
| 网络 | `network ssh` 查询 | `network ssh switch --enable/--disable` |
| 用户 | `user info` / `list` / `group` / `login-devices` | `user add` / `mod` / `del` / `set-admin` / `group` 写操作 |
| 百度网盘 | `status` / `quota` / `ls` / `search` / `info` / `task` / `sync list` | `mkdir` / `rm` / `move` / `copy` / `upload` / `download` / `share-save` / `task` / `sync` 写操作 |
| 相册 | `photos folders` / `search` / `info` / `preview` | — |

不开放的命令：`media`（本 Skill 包不提供）、交互式 `login`（只走 `login_begin` / `login_finish`）。

## 目录结构

```text
fnos_cli/
├── plugin.toml          # 插件身份、UI surface 声明、默认配置
├── __init__.py          # FnOsCliPlugin：入口点、面板上下文、写操作确认
├── _cli.py              # 启动器定位、argv 构造、异步执行与解码
├── config.example.toml  # 运行时配置模板
├── ui/panel.tsx         # Hosted TSX 面板
├── docs/quickstart.md   # 接入与授权指南
├── locales/             # zh-CN（默认）/ en
├── scripts/             # 固定版本的 fnos-cli 启动器
└── runtime-manifest.json# 启动器校验用的固定清单（SHA-256 绑定）
```

## 安装与运行

1. 确认目录名是 `fnos_cli`（与 `[plugin].id` 一致），`plugin.toml` 在其中。
2. 在 N.E.K.O 插件管理器开启「开发模式」→「加载未打包插件」，选择本目录。
3. 点击「校验」，确认 ID / 入口 / 路径后加载，再在卡片上点「启动」。
4. 打开插件面板，按 `docs/quickstart.md` 完成授权。
5. 修改源码后点卡片上的「重载」；改依赖需要重启插件。
6. 分发前点「打包」下载 `.neko-plugin`，在另一套 N.E.K.O 用普通导入入口验证。

## 启动器与首次运行

`scripts/` 下的启动器由 Skill 包原样携带，`runtime-manifest.json` 的 SHA-256 在启动器内部被硬校验。
首次执行命令时，启动器会下载与清单绑定的那一版原生 CLI（按当前平台），校验大小与摘要后解包到本机缓存，
之后复用。下载提示写入 stderr，不会混进业务输出。下载失败时重试原命令即可；提示缓存损坏时，
确认没有初始化进程在跑，再删除提示的缓存目录。

Windows 走 `scripts/fnos-cli.cmd` → 系统 PowerShell → `fnos-cli.ps1`；
macOS/Linux 走 `scripts/fnos-cli`（需要 curl、gzip、sha256sum/shasum 与常用 shell 工具）。

## 授权模型

采用 OAuth 2.0 Authorization Code + PKCE 两段式：

```text
login_begin  → 返回 authorization_url
   ↓ 用户在浏览器登录、授权，回传一次性 code
login_finish → 同一主机/用户/profile/连接参数下交换 token
```

- 插件不代填账号密码、不读取浏览器页面 code，只接收调用方主动传入的 code。
- access/refresh token 与 PKCE verifier 由 CLI 保管在受控存储，不进入对话、日志或插件状态。
- code 有效期 5 分钟、只能用一次；失败就重新生成链接，不重试旧 code。
- 切换 Host、SSH、容器或远端浏览器时，需要在该执行环境重新授权。

## 写操作确认

写操作默认要求 `confirmed=true`。未确认时入口返回：

```json
{
  "status": "clarify",
  "summary": "这是写操作，会影响 fnOS 设备状态：storage create ...。确认执行吗？",
  "context": {"command": "...", "confirmed": true}
}
```

Agent 应先向用户复述目标与影响，再带 `confirmed=true` 重调；面板则弹出确认框。
可以在配置里关闭 `require_confirmation`，但不建议——那会让所有写操作静默执行。

## 已知边界

- 面板只展示连接状态与最近一次执行结果，不缓存设备数据；每次刷新都会重新调用 `@ui.context`。
- `run` 入口不做命令白名单，只做「阻断清单 + 写操作确认」两层控制。要收紧就改 `__init__.py` 里的
  `_BLOCKED_SUBCOMMANDS` / `_WRITE_SUBCOMMANDS`。
- `photos preview --open` 需要设备侧浏览器，插件只返回地址，不负责打开。
