/**
 * 滑块事件接管 —— 本插件与 ICQQ-Plugin 之间的唯一接缝
 *
 * 做法：只监听 `system.login.slider` 事件（ICQQ-Plugin 也监听同一个），
 * 拿到 URL 就去本地服务换 ticket，然后通过云崽自带的 `verify.<QQ>` 通道
 * 把 ticket 交回给 ICQQ-Plugin 去提交。
 *
 * 为什么不直接改 ICQQ-Plugin：那是第三方插件，作者一更新改动就没了，
 * 别人也没法「一键部署」。
 *
 * 为什么 `verify.<QQ>` 是稳的：它是云崽核心
 * （`plugins/system/botOperate.js` 的 `#Bot验证` 指令）与适配器之间的既有约定，
 * 不是某个插件私有的钩子 —— ICQQ-Plugin 的 `get()` 等的就是它。
 */

import { health, depsReady, findPython } from "./service.js"
import { solveSlider } from "./solver.js"

let installed = false
/** 同一个 URL 只解一次（事件可能被重发） */
const inFlight = new Set()

function logMsg(level, msg) {
  try {
    Bot.makeLog(level, msg, "QQSlider")
  } catch {
    try {
      logger[level]?.(msg)
    } catch {
      /* 兜底：连 logger 都没有就算了 */
    }
  }
}

async function notify(msg) {
  try {
    if (typeof Bot.sendMasterMsg === "function") await Bot.sendMasterMsg(msg)
  } catch (err) {
    logMsg("error", `发送消息失败 ${err}`)
  }
}

export function baseUrlOf(cfg) {
  return `http://${cfg?.host || "127.0.0.1"}:${cfg?.port || 8767}`
}

/**
 * 从滑块 URL 里认出是哪个 QQ 在登录。
 *
 * QQ 滑块的 URL 自带 `uin` 参数；万一没有，就退回到「只有一个 bot」的情形。
 * 认不出来时返回空串，调用方会报错而不是瞎提交。
 */
export function detectUin(url) {
  try {
    const uin = new URL(url).searchParams.get("uin")
    if (uin) return String(uin)
  } catch {
    /* URL 畸形就往下退 */
  }
  const uins = [...(Bot.uin || [])].filter(Boolean)
  if (uins.length === 1) return String(uins[0])
  const ids = Object.keys(Bot.bots || {})
  if (ids.length === 1) return ids[0]
  return ""
}

/**
 * 把 ticket 交给适配器提交。
 *
 * 优先走 `verify.<QQ>`：ICQQ-Plugin 的默认分支拿到 msg 就直接
 * `bot.submitSlider(msg)`，这样它的等待循环会正常结束、不会 3 分钟后
 * 再报一句「滑动验证超时」。
 * 没有监听者时直接调 `submitSlider`（JiuLi / 其它框架上更保险）。
 */
async function submitTicket(id, ticket) {
  const event = `verify.${id}`
  if (typeof Bot.listenerCount === "function" && Bot.listenerCount(event) > 0) {
    Bot.em(event, { msg: ticket, reply: m => notify(m) })
    return "verify"
  }
  const bot = Bot[id]
  if (bot && typeof bot.submitSlider === "function") {
    await bot.submitSlider(ticket)
    return "direct"
  }
  throw new Error("找不到可用的提交通道（适配器还没就绪？）")
}

/**
 * 装上滑块接管。重复调用只生效一次。
 *
 * @param {object} opts
 * @param {() => object} opts.getConfig 取当前配置
 * @returns {boolean} 是否是本次装上的
 */
export function installSliderHook({ getConfig }) {
  if (installed) return false
  installed = true

  Bot.on("system.login.slider", async data => {
    const cfg = getConfig() || {}
    if (!cfg.enable) return

    const url = data?.url
    if (!url) return
    if (inFlight.has(url)) return
    inFlight.add(url)

    const id = detectUin(url)
    const baseUrl = baseUrlOf(cfg)
    try {
      // 手动模式：只把 URL 发出来，不接管（交给 ICQQ-Plugin 原来的选单）
      if (cfg.mode !== "auto") {
        logMsg("info", `手动模式，滑块 URL：${url}`)
        return
      }

      logMsg("info", `收到滑块验证（uin=${id || "未知"}），开始自动过码`)
      await notify(`[${id || "?"}] 正在自动过码，请稍候…`)

      const ticket = await solveSlider({
        url,
        uin: id,
        baseUrl,
        port: cfg.port,
        timeout: cfg.timeout,
        logger: { debug: m => logMsg("debug", m) },
      })
      if (!id) throw new Error("认不出是哪个 QQ 在登录，无法提交 ticket")

      const via = await submitTicket(id, ticket)
      logMsg("info", `自动过码成功（${via}）ticket=${String(ticket).slice(0, 16)}…`)
      await notify(`[${id}] 自动过码成功`)
    } catch (err) {
      logMsg("error", `自动过码失败：${err?.stack || err}`)
      await notify(
        `[${id || "?"}] 自动过码失败：${err?.message || err}\n` +
          `可手动完成验证：\n${url}`,
      )
    } finally {
      inFlight.delete(url)
    }
  })

  logMsg("info", "已接管滑块验证（system.login.slider）")
  return true
}

/** 汇总当前状态，给 #滑块过码状态 用 */
export async function getStatus(cfg) {
  const baseUrl = baseUrlOf(cfg)
  let running = null
  try {
    running = await health(baseUrl)
  } catch {
    running = null
  }
  return {
    hooked: installed,
    enabled: !!cfg?.enable,
    mode: cfg?.mode || "auto",
    baseUrl,
    depsReady: depsReady(),
    python: findPython(),
    running: !!running,
    health: running,
  }
}
