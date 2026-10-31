import {
  Page,
  Card,
  Grid,
  Stack,
  Inline,
  Text,
  Tip,
  Alert,
  StatCard,
  StatusBadge,
  DataTable,
  Button,
  Field,
  Input,
  Select,
  Switch,
  RefreshButton,
  useForm,
  useEffect,
  useState,
  useToast,
  useConfirm,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type FnosConfig = {
  host?: string
  port?: number
  scheme?: string
  profile?: string
  require_confirmation?: boolean
  timeout_seconds?: number
  working_dir?: string
  return_raw_output?: boolean
  allow_insecure_http?: boolean
  tls_insecure?: boolean
}

type PlatformInfo = {
  os?: string
  arch?: string
  launcher_ok?: boolean
  launcher?: string
  launcher_error?: string
  cli_version?: string
}

type QuickCommand = {
  group?: string
  subcommands?: string[]
}

type DashboardState = {
  config?: FnosConfig
  platform?: PlatformInfo
  launcher_ok?: boolean
  last_result?: Record<string, any> | null
  quick_commands?: QuickCommand[]
}

type PendingLogin = {
  url: string
  host: string
  port: number
  scheme: string
  profile: string
  allowInsecureHttp?: boolean
  tlsInsecure?: boolean
}

type LogRow = {
  key: string
  level?: string
  message?: string
}

const defaultConfigForm = {
  host: "",
  port: "5666",
  scheme: "auto",
  profile: "home",
  require_confirmation: true,
  timeout_seconds: "300",
  working_dir: "",
  allow_insecure_http: false,
  tls_insecure: false,
}

const emptyCommandForm = {
  command: "system info",
}

const emptyLoginForm = {
  host: "",
  port: "5666",
  scheme: "auto",
  profile: "home",
}

const emptyCodeForm = {
  code: "",
}

const READ_ONLY_PRESETS = [
  { group: "system", sub: "info", label: "系统信息" },
  { group: "storage", sub: "overview", label: "存储概览" },
  { group: "storage", sub: "pools", label: "存储池" },
  { group: "storage", sub: "disks", label: "磁盘列表" },
  { group: "monitor", sub: "cpu", label: "CPU" },
  { group: "monitor", sub: "memory", label: "内存" },
  { group: "download", sub: "ls", label: "下载任务" },
  { group: "docker", sub: "stats", label: "Docker" },
  { group: "file", sub: "ls", label: "根目录文件" },
]

type ConfigFormValues = typeof defaultConfigForm
type CommandFormValues = typeof emptyCommandForm
type LoginFormValues = typeof emptyLoginForm
type CodeFormValues = typeof emptyCodeForm

function actionById(actions: HostedAction[], id: string): HostedAction | undefined {
  return actions.find((action) => action.id === id || action.entry_id === id)
}

function unwrapActionResult(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    if (envelope.result && typeof envelope.result === "object") return envelope.result
    return envelope
  }
  return {}
}

function requireReady(result: Record<string, any>): Record<string, any> {
  if (result.status === "ready" || result.status === "ok") return result
  throw new Error(String(result.summary || result.message || result.status || "Action failed"))
}

async function callConfirmed(
  props: PluginSurfaceProps<DashboardState>,
  actionId: string,
  payload: Record<string, any>,
): Promise<Record<string, any>> {
  const first = unwrapActionResult(await props.api.call(actionId, { ...payload, confirmed: true }))
  if (first.status === "clarify" && first.context) {
    const confirmed = unwrapActionResult(
      await props.api.call(actionId, { ...first.context, confirmed: true }),
    )
    return requireReady(confirmed)
  }
  return requireReady(first)
}

export default function FnosPanel(props: PluginSurfaceProps<DashboardState>) {
  const { state, actions, t } = props
  const safeState = state || {}
  const config = safeState.config || {}
  const platform = safeState.platform || {}
  const launcherOk = safeState.launcher_ok !== false && platform.launcher_ok !== false
  const lastResult = safeState.last_result
  const toast = useToast()
  const confirm = useConfirm()
  const configForm = useForm<ConfigFormValues>(defaultConfigForm)
  const commandForm = useForm<CommandFormValues>(emptyCommandForm)
  const loginForm = useForm<LoginFormValues>(emptyLoginForm)
  const codeForm = useForm<CodeFormValues>(emptyCodeForm)
  const [pendingLogin, setPendingLogin] = useState<PendingLogin | null>(null)

  const updateConfigAction = actionById(actions || [], "update_config")
  const runAction = actionById(actions || [], "run")
  const loginBeginAction = actionById(actions || [], "login_begin")
  const loginFinishAction = actionById(actions || [], "login_finish")
  const logoutAction = actionById(actions || [], "logout")
  const statusAction = actionById(actions || [], "status")
  const readActions = READ_ONLY_PRESETS.map((preset) => ({
    preset,
    action: actionById(
      actions || [],
      preset.group === "file" ? "run" : preset.group === "system"
        ? "system_info"
        : preset.group === "storage" && preset.sub === "overview"
          ? "storage_overview"
          : preset.group === "storage" && preset.sub === "pools"
            ? "storage_pools"
            : preset.group === "storage" && preset.sub === "disks"
              ? "storage_disks"
              : preset.group === "monitor" && preset.sub === "cpu"
                ? "monitor_cpu"
                : preset.group === "monitor" && preset.sub === "memory"
                  ? "monitor_memory"
                  : preset.group === "download"
                    ? "download_list"
                    : preset.group === "docker"
                      ? "docker_stats"
                      : "run",
    ),
  }))

  useEffect(() => {
    configForm.setValues({
      host: String(config.host || ""),
      port: String(config.port ?? 5666),
      scheme: String(config.scheme || "auto"),
      profile: String(config.profile || "home"),
      require_confirmation: config.require_confirmation !== false,
      timeout_seconds: String(config.timeout_seconds ?? 300),
      working_dir: String(config.working_dir || ""),
      allow_insecure_http: config.allow_insecure_http === true,
      tls_insecure: config.tls_insecure === true,
    })
    loginForm.setValues({
      host: String(config.host || ""),
      port: String(config.port ?? 5666),
      scheme: String(config.scheme || "auto"),
      profile: String(config.profile || "home"),
    })
  }, [
    config.host,
    config.port,
    config.scheme,
    config.profile,
    config.require_confirmation,
    config.timeout_seconds,
    config.working_dir,
  ])

  async function saveConfig() {
    if (!updateConfigAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    try {
      const timeoutSeconds = Number(configForm.values.timeout_seconds)
      if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 5) {
        throw new Error(t("panel.errors.timeoutRange"))
      }
      await callConfirmed(props, "update_config", {
        host: configForm.values.host.trim(),
        port: Number(configForm.values.port) || 5666,
        scheme: configForm.values.scheme,
        profile: configForm.values.profile.trim() || "home",
        require_confirmation: !!configForm.values.require_confirmation,
        timeout_seconds: timeoutSeconds,
        working_dir: configForm.values.working_dir.trim(),
        allow_insecure_http: !!configForm.values.allow_insecure_http,
        tls_insecure: !!configForm.values.tls_insecure,
      })
      await props.api.refresh()
      toast.success(t("panel.messages.configSaved"))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function runCommand() {
    if (!runAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    const command = commandForm.values.command.trim()
    if (!command) {
      toast.error(t("panel.errors.commandRequired"))
      return
    }
    try {
      const result = await callConfirmed(props, "run", { command })
      if (result.status === "error") {
        toast.error(String(result.summary || t("panel.errors.commandFailed")))
      } else {
        toast.success(String(result.summary || t("panel.messages.commandDone")))
      }
      await props.api.refresh()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function runPreset(group: string, sub: string, entryId: string) {
    if (!entryId) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    try {
      if (entryId === "run") {
        await callConfirmed(props, "run", { command: `${group} ${sub}` })
      } else {
        await props.api.call(entryId, {})
      }
      await props.api.refresh()
      toast.success(t("panel.messages.commandDone"))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function beginLogin() {
    if (!loginBeginAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    const host = loginForm.values.host.trim()
    const port = Number(loginForm.values.port) || 5666
    const scheme = loginForm.values.scheme
    const profile = loginForm.values.profile.trim() || "home"
    try {
      const result = unwrapActionResult(
        await props.api.call("login_begin", { host, port, scheme, profile }),
      )
      requireReady(result)
      const url = String(result.authorization_url || "")
      if (!url) {
        throw new Error(String(result.summary || t("panel.errors.loginFailed")))
      }
      // 链接必须显示在面板上：弹窗可能被浏览器拦截，用户也可能想把链接
      // 复制到手机或另一台设备打开。只 window.open 的话链接就看不见了。
      // 连接参数用后端回显的生效值，而不是表单原值 —— 空表单会走插件默认，
      // 且 allow_insecure_http / tls_insecure 只有后端知道。
      const conn = (result.connection || {}) as Record<string, any>
      setPendingLogin({
        url,
        host: String(conn.host ?? host),
        port: Number(conn.port ?? port),
        scheme: String(conn.scheme ?? scheme),
        profile: String(conn.profile ?? profile),
        allowInsecureHttp: conn.allow_insecure_http === true,
        tlsInsecure: conn.tls_insecure === true,
      })
      toast.success(t("panel.messages.linkGenerated"))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function finishLogin() {
    if (!loginFinishAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    const code = codeForm.values.code.trim()
    if (!code) {
      toast.error(t("panel.errors.codeRequired"))
      return
    }
    // 沿用 login_begin 那次生效的连接参数：Skill 要求两次调用保持一致，
    // 否则 pending verifier 对不上、code 兑换失败。用户中途改了表单也没用。
    const conn = pendingLogin || {
      host: loginForm.values.host.trim(),
      port: Number(loginForm.values.port) || 5666,
      scheme: loginForm.values.scheme,
      profile: loginForm.values.profile.trim() || "home",
    }
    try {
      const result = unwrapActionResult(
        await props.api.call("login_finish", {
          code,
          host: conn.host,
          port: conn.port,
          scheme: conn.scheme,
          profile: conn.profile,
        }),
      )
      requireReady(result)
      toast.success(String(result.summary || t("panel.messages.loginDone")))
      codeForm.setValues({ code: "" })
      setPendingLogin(null)
      await props.api.refresh()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  async function doLogout() {
    if (!logoutAction) {
      toast.error(t("panel.errors.actionUnavailable"))
      return
    }
    const ok = await confirm({
      title: t("panel.actions.logout"),
      message: t("panel.confirmLogout"),
      tone: "danger",
      confirmLabel: t("panel.actions.logout"),
      cancelLabel: t("panel.actions.cancel"),
    })
    if (!ok) return
    try {
      await callConfirmed(props, "logout", {})
      await props.api.refresh()
      toast.success(t("panel.messages.logoutDone"))
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err))
    }
  }

  const logRows: LogRow[] = lastResult
    ? [
        {
          key: "last",
          level: lastResult.status === "ok" ? "ok" : "error",
          message: String(lastResult.summary || lastResult.command || ""),
        },
      ]
    : []

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Grid cols={4}>
        <StatCard
          label={t("panel.stats.launcher")}
          value={
            <StatusBadge
              tone={launcherOk ? "success" : "danger"}
              label={launcherOk ? t("panel.launcher.ready") : t("panel.launcher.missing")}
            />
          }
        />
        <StatCard label={t("panel.stats.cliVersion")} value={platform.cli_version || "-"} />
        <StatCard
          label={t("panel.stats.target")}
          value={`${config.host || "localhost"}:${config.port ?? 5666}`}
        />
        <StatCard label={t("panel.stats.profile")} value={config.profile || "home"} />
      </Grid>

      {!launcherOk ? (
        <Alert tone="danger">
          {t("panel.launcher.missingDetail")}
          {platform.launcher_error ? ` (${platform.launcher_error})` : ""}
        </Alert>
      ) : null}

      <Grid cols={2}>
        <Card title={t("panel.connection.title")}>
          <Stack>
            <Field label={t("panel.fields.host")} help={t("panel.fields.hostHelp")}>
              <Input
                value={loginForm.values.host}
                placeholder="192.168.1.10 或 nas.example.com"
                onChange={(value) => loginForm.setField("host", value)}
              />
            </Field>
            <Field label={t("panel.fields.port")}>
              <Input
                value={loginForm.values.port}
                placeholder="5666"
                onChange={(value) => loginForm.setField("port", value)}
              />
            </Field>
            <Field label={t("panel.fields.scheme")}>
              <Select
                value={loginForm.values.scheme}
                options={["auto", "http", "https"]}
                onChange={(value) => loginForm.setField("scheme", String(value))}
              />
            </Field>
            <Field label={t("panel.fields.profile")} help={t("panel.fields.profileHelp")}>
              <Input
                value={loginForm.values.profile}
                placeholder="home"
                onChange={(value) => loginForm.setField("profile", value)}
              />
            </Field>
            <Stack>
              <Button
                tone="primary"
                disabled={!loginBeginAction}
                onClick={beginLogin}
              >
                {t("panel.actions.beginLogin")}
              </Button>
            </Stack>
            {pendingLogin ? (
              <Card title={t("panel.login.pendingTitle")}>
                <Stack>
                  <Alert tone="info" message={t("panel.login.pendingHint")} />
                  {/* 链接原文可见、可选中复制；window.open 可能被浏览器拦截，
                      用户也可能要在手机或另一台设备上打开。 */}
                  <Text>{pendingLogin.url}</Text>
                  {/* 浏览器打不开链接时，先看这里：scheme=auto 会把远端 IP
                      推断成 https，而局域网 fnOS 多是 http。 */}
                  <Alert
                    tone={pendingLogin.allowInsecureHttp || pendingLogin.tlsInsecure ? "success" : "warning"}
                    message={
                      pendingLogin.allowInsecureHttp || pendingLogin.tlsInsecure
                        ? t("panel.login.connectionOk")
                        : t("panel.login.connectionWarn")
                    }
                  />
                  <Text>{`${pendingLogin.host}:${pendingLogin.port} · ${pendingLogin.scheme} · ${pendingLogin.profile}`}</Text>
                  <Inline gap={8} wrap>
                    <Button
                      tone="primary"
                      onClick={() =>
                        window.open(pendingLogin.url, "_blank", "noopener,noreferrer")
                      }
                    >
                      {t("panel.login.openLink")}
                    </Button>
                    <Button
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(pendingLogin.url)
                          toast.success(t("panel.messages.linkCopied"))
                        } catch {
                          toast.error(t("panel.errors.copyFailed"))
                        }
                      }}
                    >
                      {t("panel.login.copyLink")}
                    </Button>
                  </Inline>
                  <Field label={t("panel.fields.code")} help={t("panel.login.codeHelp")}>
                    <Input
                      value={codeForm.values.code}
                      placeholder={t("panel.login.codePlaceholder")}
                      onChange={(value) => codeForm.setField("code", value)}
                    />
                  </Field>
                  <Inline gap={8} wrap>
                    <Button
                      tone="success"
                      disabled={!loginFinishAction}
                      onClick={finishLogin}
                    >
                      {t("panel.actions.finishLogin")}
                    </Button>
                    <Button
                      onClick={() => {
                        setPendingLogin(null)
                        codeForm.setValues({ code: "" })
                      }}
                    >
                      {t("panel.actions.cancel")}
                    </Button>
                  </Inline>
                </Stack>
              </Card>
            ) : null}
            <Stack>
              <Button tone="danger" disabled={!logoutAction} onClick={doLogout}>
                {t("panel.actions.logout")}
              </Button>
            </Stack>
            <Tip>{t("panel.connection.tip")}</Tip>
          </Stack>
        </Card>

        <Card title={t("panel.config.title")}>
          <Stack>
            <Field label={t("panel.fields.host")}>
              <Input
                value={configForm.values.host}
                placeholder="192.168.1.10"
                onChange={(value) => configForm.setField("host", value)}
              />
            </Field>
            <Field label={t("panel.fields.port")}>
              <Input
                value={configForm.values.port}
                placeholder="5666"
                onChange={(value) => configForm.setField("port", value)}
              />
            </Field>
            <Field label={t("panel.fields.scheme")}>
              <Select
                value={configForm.values.scheme}
                options={["auto", "http", "https"]}
                onChange={(value) => configForm.setField("scheme", String(value))}
              />
            </Field>
            <Field label={t("panel.fields.profile")}>
              <Input
                value={configForm.values.profile}
                placeholder="home"
                onChange={(value) => configForm.setField("profile", value)}
              />
            </Field>
            <Field label={t("panel.fields.timeout")} help={t("panel.fields.timeoutHelp")}>
              <Input
                value={configForm.values.timeout_seconds}
                placeholder="300"
                onChange={(value) => configForm.setField("timeout_seconds", value)}
              />
            </Field>
            <Field label={t("panel.fields.workingDir")} help={t("panel.fields.workingDirHelp")}>
              <Input
                value={configForm.values.working_dir}
                placeholder={t("panel.placeholders.workingDir")}
                onChange={(value) => configForm.setField("working_dir", value)}
              />
            </Field>
            <Switch
              checked={configForm.values.require_confirmation}
              label={t("panel.fields.requireConfirmation")}
              onChange={(value) => configForm.setField("require_confirmation", value)}
            />
            {/* Switch 没有 help prop，说明文字用 Tip 补。 */}
            <Switch
              checked={configForm.values.allow_insecure_http}
              label={t("panel.fields.allowInsecureHttp")}
              onChange={(value) => configForm.setField("allow_insecure_http", value)}
            />
            <Tip>{t("panel.fields.allowInsecureHttpHelp")}</Tip>
            <Switch
              checked={configForm.values.tls_insecure}
              label={t("panel.fields.tlsInsecure")}
              onChange={(value) => configForm.setField("tls_insecure", value)}
            />
            <Tip>{t("panel.fields.tlsInsecureHelp")}</Tip>
            <Button tone="success" disabled={!updateConfigAction} onClick={saveConfig}>
              {t("panel.actions.saveConfig")}
            </Button>
            <Tip>{t("panel.config.tip")}</Tip>
          </Stack>
        </Card>
      </Grid>

      <Card title={t("panel.quick.title")}>
        <Stack>
          <Text>{t("panel.quick.description")}</Text>
          <Grid cols={3}>
            {readActions.map(({ preset, action }) => (
              <Button
                key={`${preset.group}-${preset.sub}`}
                tone="default"
                disabled={!action || !launcherOk}
                onClick={() => runPreset(preset.group, preset.sub, action?.id || action?.entry_id || "")}
              >
                {preset.label}
              </Button>
            ))}
          </Grid>
          <RefreshButton label={t("panel.actions.refresh")} />
        </Stack>
      </Card>

      <Card title={t("panel.command.title")}>
        <Stack>
          <Field label={t("panel.fields.command")} help={t("panel.fields.commandHelp")}>
            <Input
              value={commandForm.values.command}
              placeholder="storage pools"
              onChange={(value) => commandForm.setField("command", value)}
            />
          </Field>
          <Button tone="primary" disabled={!runAction || !launcherOk} onClick={runCommand}>
            {t("panel.actions.run")}
          </Button>
          <Tip>{t("panel.command.tip")}</Tip>
        </Stack>
      </Card>

      <Card title={t("panel.last.title")}>
        <Stack>
          {lastResult ? (
            <>
              <StatusBadge
                tone={lastResult.status === "ok" ? "success" : "warning"}
                label={String(lastResult.status || "-")}
              />
              <DataTable
                data={logRows}
                rowKey="key"
                columns={[
                  { key: "level", label: t("panel.columns.level") },
                  { key: "message", label: t("panel.columns.message") },
                ]}
                emptyText={t("panel.last.empty")}
              />
            </>
          ) : (
            <Text>{t("panel.last.empty")}</Text>
          )}
        </Stack>
      </Card>
    </Page>
  )
}
