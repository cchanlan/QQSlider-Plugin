logger.info(logger.yellow("- 正在加载 QQ 滑块过码插件"))

import makeConfig from "../../lib/plugins/config.js"
import { installSliderHook, getStatus, baseUrlOf } from "./utils/hook.js"
import { installDeps, depsReady, findPython, health, ensureRunning } from "./utils/service.js"

const { config, configSave } = await makeConfig(
  "QQSlider",
  {
    tips: "",
    permission: "master",
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
  },
  {
    tips: [
      "QQ 滑块自动过码插件",
      "装好后无需配置：云崽登录遇到滑块时会自动过码",
      "发送 #滑块过码状态 查看运行情况",
    ],
  },
)

function logMsg(level, msg) {
  try {
    Bot.makeLog(level, msg, "QQSlider")
  } catch {
    logger[level]?.(msg)
  }
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
          reg: "^#滑块过码测试$",
          fnc: "Test",
          permission: config.permission,
        },
        {
          reg: "^#滑块过码(开启|关闭)$",
          fnc: "Toggle",
          permission: config.permission,
        },
      ],
    })
  }

  async init() {
    // 只装一次（插件类会被实例化两次）
    installSliderHook({ getConfig: () => config })

    // 首次自动装依赖：放后台，绝不阻塞云崽启动
    if (config.enable && config.autoInstall && !depsReady()) {
      setTimeout(async () => {
        if (!findPython()) {
          logMsg("error", "没找到 Python，请装好 Python 3.9+ 后发 #滑块过码安装")
          return
        }
        const r = await installDeps({ logger: { info: m => logMsg("info", m), debug: m => logMsg("debug", m) } })
        if (!r.ok) {
          logMsg("error", `依赖安装失败：${r.error}`)
          return
        }
        await ensureRunning({
          baseUrl: baseUrlOf(config),
          port: config.port,
          rounds: config.rounds,
          logger: { info: m => logMsg("info", m), warn: m => logMsg("warn", m) },
        })
      }, 3000)
    } else if (config.enable) {
      // 依赖已就绪，直接把服务拉起来（后台）
      setTimeout(() => {
        ensureRunning({
          baseUrl: baseUrlOf(config),
          port: config.port,
          rounds: config.rounds,
          logger: { info: m => logMsg("info", m), warn: m => logMsg("warn", m) },
        }).catch(err => logMsg("error", `服务启动失败：${err}`))
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
      `Python：${s.python || "未找到"}`,
    ]
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
    const r = await installDeps({
      logger: { info: m => logMsg("info", m), debug: m => logMsg("debug", m) },
      force: false,
    })
    if (!r.ok) {
      await this.reply(`安装失败：${r.error}`, true)
      return
    }
    const started = await ensureRunning({
      baseUrl: baseUrlOf(config),
      port: config.port,
      rounds: config.rounds,
      logger: { info: m => logMsg("info", m), warn: m => logMsg("warn", m) },
    })
    await this.reply(started.ok ? "过码环境已就绪，登录遇到滑块会自动过码" : `服务启动失败：${started.error}`, true)
  }

  async Test() {
    const baseUrl = baseUrlOf(config)
    if (!(await health(baseUrl))) {
      await this.reply("过码服务没在运行，先发 #滑块过码安装", true)
      return
    }
    await this.reply("正在自测（会真实解一次验证码，约 3~10 秒）…", true)
    try {
      const res = await fetch(`${baseUrl}/solve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rounds: config.rounds }),
        signal: AbortSignal.timeout(config.timeout),
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
}

logger.info(logger.green("- QQ 滑块过码插件 加载完成"))
