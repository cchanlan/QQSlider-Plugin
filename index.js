import { makePluginConfig } from "./utils/config.js"
import { installSliderHook, getStatus, baseUrlOf, masterIds } from "./utils/hook.js"
import { installDeps, depsReady, health, ensureRunning, restartService, codeHashInfo } from "./utils/service.js"

logger.info(logger.yellow("- 正在加载 QQ 滑块过码插件"))

const { config, configSave } = await makePluginConfig(
  "QQSlider",
  {
    // 总开关
    enable: true,
    // auto = 自动过码；manual = 只把验证链接发出来（自己手动过）
    mode: "auto",
    host: "127.0.0.1",
    port: 8767,
    // 每次过码最多试几轮
    rounds: 3,
    // 单次过码超时（毫秒）
    timeout: 180000,
    // 首次加载自动装 Python 依赖（约 1~3 分钟，后台进行）
    autoInstall: true,
    // 指令权限：master / admin / all
    permission: "master",
    // 过码通知发给哪个主人号（空 = 用框架配置里的主人，多个 bot 在线时可能各发一份）
    notifyMaster: "",
  },
)

function logMsg(level, msg) {
  try {
    Bot.makeLog(level, msg, "QQSlider")
  } catch {
    logger[level]?.(msg)
  }
}

const svcLogger = {
  info: m => logMsg("info", m),
  warn: m => logMsg("warn", m),
  debug: m => logMsg("debug", m),
}

export class QQSlider extends plugin {
  constructor() {
    super({
      name: "QQ滑块过码",
      dsc: "自动通过 icqq 登录时的滑块验证",
      event: "message",
      priority: -1000,
      rule: [
        {
          reg: "^#滑块过码(状态|信息)$",
          fnc: "Status",
          permission: config.permission,
        },
        {
          reg: "^#滑块过码安装$",
          fnc: "Install",
          permission: config.permission,
        },
        {
          // 别名写成两组，让「重启服务」这件事好记：
          // 更新完插件后过码行为还是老的，就发这条
          reg: "^#滑块过码(重启|重载|更新服务)$",
          fnc: "Restart",
          permission: config.permission,
        },
        {
          reg: "^#滑块过码测试$",
          fnc: "Test",
          permission: config.permission,
        },
        {
          reg: "^#滑块过码(开启|关闭)$",
          fnc: "Toggle",
          permission: config.permission,
        },
        {
          // 指定过码通知发给哪个号（多个 bot 在线时用得上）
          reg: "^#滑块过码通知",
          fnc: "NotifyMaster",
          permission: config.permission,
        },
      ],
    })
  }

  async init() {
    // 只装一次（插件类会被实例化两次）
    installSliderHook({ getConfig: () => config })

    if (!config.enable) return

    // 首次自动装依赖：放后台，绝不阻塞云崽启动
    if (config.autoInstall && !depsReady()) {
      setTimeout(async () => {
        const r = await installDeps({ logger: svcLogger })
        if (!r.ok) {
          logMsg("error", `依赖安装失败：${r.error}`)
          return
        }
        await ensureRunning({
          baseUrl: baseUrlOf(config),
          port: config.port,
          rounds: config.rounds,
          logger: svcLogger,
        })
      }, 3000)
    } else if (depsReady()) {
      // 依赖已就绪，直接把服务拉起来（后台）。
      //
      // ⚠️ 这里同时承担**更新后换掉旧服务**的职责：`ensureRunning` 会比对
      // 磁盘代码指纹和正在跑的服务的指纹，不一致就重启服务。
      // 所以用户「更新插件 → 重启云崽」之后，**不用再做任何事**，
      // Python 侧的新代码会在这一步自动生效（见 utils/service.js 的 ensureRunning）。
      setTimeout(() => {
        ensureRunning({
          baseUrl: baseUrlOf(config),
          port: config.port,
          rounds: config.rounds,
          logger: svcLogger,
        }).catch(err => logMsg("error", `服务启动失败：${err?.message || err}`))
      }, 3000)
    }
  }

  async Status() {
    const s = await getStatus(config)
    const lines = [
      `状态：${s.enabled ? (s.mode === "auto" ? "已开启（自动过码）" : "已开启（手动模式）") : "已关闭"}`,
      `接管：${s.hooked ? "已接管滑块验证" : "未接管"}`,
      `服务：${s.running ? "运行中" : "未运行"}  ${s.baseUrl}`,
      `依赖：${s.depsReady ? "已就绪" : "未安装"}`,
      `Python：${s.env?.python || "未找到"}`,
      `Node：${s.env?.node || "未找到"}`,
    ]
    // 多 bot 在线时，过码通知发给谁
    {
      const cur = String(config.notifyMaster || "").trim()
      if (cur) {
        lines.push(`通知：只发给 ${cur}`)
      } else {
        const fromCfg = await masterIds(config)
        lines.push(
          fromCfg.length
            ? `通知：框架主人 ${fromCfg.join("、")}`
            : "通知：没读到主人号（发 #滑块过码通知 <QQ号> 指定）",
        )
      }
    }
    // 版本比对：跑的是不是磁盘上这份代码
    if (s.running) {
      const v = await codeHashInfo(s.baseUrl)
      if (!v.ours) {
        // 端口上有东西在响应，但不是本插件 —— 多半是端口被别的程序占了
        lines.push(`版本：${s.baseUrl} 被别的程序占用（改 config 里的 port 或换一个）`)
      } else if (v.stale) {
        lines.push(`版本：服务跑的是旧代码（发 #滑块过码重启 更新）`)
      } else if (v.known) {
        lines.push(`版本：已是最新`)
      }
    }
    // 点选识别能力：查服务报回来的模型状态
    const click = s.health?.env?.click
    if (click) {
      lines.push(
        `点选：${click.modelReady ? `可以过（识别模型 ${click.modelMB}MB）` : "未就绪（发 #滑块过码安装）"}`,
      )
    }
    if (s.env?.missing?.length) lines.push(`缺少：${s.env.missing.join("、")}`)
    if (s.health) {
      lines.push(
        `统计：成功 ${s.health.ok} / 失败 ${s.health.fail}` +
          (s.health.avgSeconds ? `，平均 ${s.health.avgSeconds}s` : ""),
      )
    }
    await this.reply(lines.join("\n"), true)
  }

  async Install() {
    await this.reply("开始准备过码环境（首次约 1~3 分钟），完成后会通知你", true)
    const r = await installDeps({ logger: svcLogger, force: false })
    if (!r.ok) {
      await this.reply(`安装失败：${r.error}`, true)
      return
    }
    const started = await ensureRunning({
      baseUrl: baseUrlOf(config),
      port: config.port,
      rounds: config.rounds,
      logger: svcLogger,
    })
    await this.reply(started.ok ? "过码环境已就绪，登录遇到滑块会自动过码" : `服务启动失败：${started.error}`, true)
  }

  async Restart() {
    await this.reply("正在重启过码服务…", true)
    const stopped = await restartService({
      baseUrl: baseUrlOf(config),
      port: config.port,
      logger: svcLogger,
    })
    if (!stopped) {
      await this.reply("旧服务停不掉（端口可能被别的进程占着），重启云崽后再试", true)
      return
    }
    const started = await ensureRunning({
      baseUrl: baseUrlOf(config),
      port: config.port,
      rounds: config.rounds,
      logger: svcLogger,
    })
    await this.reply(started.ok ? "过码服务已重启，跑的是最新代码" : `重启失败：${started.error}`, true)
  }

  async Test() {
    const baseUrl = baseUrlOf(config)
    if (!(await health(baseUrl))) {
      const started = await ensureRunning({
        baseUrl,
        port: config.port,
        rounds: config.rounds,
        logger: svcLogger,
      })
      if (!started.ok) {
        await this.reply(`过码服务没起来：${started.error}`, true)
        return
      }
    }
    await this.reply("正在自测（会真实解一次验证码，约 3~10 秒）…", true)
    try {
      const res = await fetch(`${baseUrl}/solve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rounds: config.rounds }),
        signal: AbortSignal.timeout(Number(config.timeout) || 180000),
      })
      const data = await res.json()
      if (data.ok) {
        await this.reply(`自测通过：${data.seconds}s 拿到 ticket\n${String(data.ticket).slice(0, 40)}…`, true)
      } else {
        await this.reply(`自测失败：${data.error || data.errorCode}`, true)
      }
    } catch (err) {
      await this.reply(`自测出错：${err?.message || err}`, true)
    }
  }

  async Toggle() {
    config.enable = this.e.msg.includes("开启")
    await configSave()
    await this.reply(`滑块过码已${config.enable ? "开启" : "关闭"}`, true)
  }

  /**
   * `#滑块过码通知`            —— 看当前通知发给谁
   * `#滑块过码通知 <QQ号>`     —— **指定过码通知只发给这个号**
   * `#滑块过码通知 默认`       —— 清空设置，用框架配置里的主人
   *
   * 为什么需要：框架的 `sendMasterMsg` 是**广播** —— 多个 bot 同时在线时
   * 主人会连着收到好几份一模一样的过码通知（实测 4 个 bot = 4 条）。
   */
  async NotifyMaster() {
    const arg = String(this.e.msg || "")
      .replace(/^#滑块过码通知/, "")
      .trim()

    const cur = String(config.notifyMaster || "").trim()

    if (!arg) {
      const fromCfg = await masterIds(config)
      await this.reply(
        [
          `过码通知发给：${cur ? cur : "跟框架配置（见下）"}`,
          fromCfg.length ? `框架里的主人号：${fromCfg.join("、")}` : "框架里没读到主人号",
          "",
          "发 #滑块过码通知 <QQ号> 指定只发给谁；发 #滑块过码通知 默认 恢复",
        ].join("\n"),
        true,
      )
      return
    }

    if (arg === "默认" || arg === "清除" || arg === "取消") {
      config.notifyMaster = ""
      await configSave()
      await this.reply("已恢复：通知按框架配置的主人号发", true)
      return
    }

    const id = arg.match(/\d{5,12}/)?.[0] || ""
    if (!id) {
      await this.reply(`认不出 QQ 号：${arg}`, true)
      return
    }

    config.notifyMaster = id
    await configSave()
    await this.reply(`过码通知已改为只发给 ${id}`, true)
  }
}

logger.info(logger.green("- QQ 滑块过码插件 加载完成"))
