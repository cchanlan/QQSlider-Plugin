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
 *
 * ⚠️ TRSS-Yunzai / Miao-Yunzai / JiuLi 三家的 `Bot` 都是 EventEmitter，
 * 但挂在上面的方法名不完全一样（JiuLi 的 Bot 是 Proxy，属性会回落到 util）。
 * 所以这里**一律能力探测 + 多级兜底**，不假设某个方法一定存在。
 */

import { health, depsReady, envReport } from "./service.js"
import { solveSlider } from "./solver.js"

let installed = false
/** 同一个 URL 只解一次（事件可能被重发） */
const inFlight = new Set()

function logMsg(level, msg) {
  try {
    Bot.makeLog(level, msg, "QQSlider")
    return
  } catch {
    /* 落到下面的兜底 */
  }
  try {
    logger[level]?.(msg)
  } catch {
    /* 兜底：连 logger 都没有就算了 */
  }
}

/** 取 bots 里的账号 id 列表（三家框架的 `Bot.bots` 都是 { [id]: bot }） */
function botIds() {
  try {
    return Object.keys(Bot?.bots || {}).filter(Boolean)
  } catch {
    return []
  }
}

/**
 * 按 QQ 号取「那个 bot 自己」的适配器实例。
 *
 * ⚠️ **两家框架挂的位置不一样，必须都查**：
 *   · TRSS / Miao-Yunzai：`Bot[uin]` 就是适配器实例（Bot 是个对象/Proxy）
 *   · 部分形态 / JiuLi 包装后：适配器挂在 `Bot.bots[uin]` 下
 *
 * 只查其中一个的话，另一个框架上就会「找不到 bot → 回落成广播」，
 * 也就是主人收到好几份通知的老毛病照旧。
 * （这个 bug 是 `test_notify.mjs` 抓出来的：mock 只挂 `Bot.bots` 时功能失效。）
 *
 * @returns {object|null}
 */
function resolveBot(id) {
  const key = String(id || "").trim()
  if (!key) return null
  for (const src of [() => Bot?.[key], () => Bot?.bots?.[key]]) {
    try {
      const b = src()
      if (b && typeof b === "object") return b
    } catch {
      /* 某个形态取不到就试下一个 */
    }
  }
  return null
}

/**
 * 列出可用的 bot，给「指定用哪个 bot 发主人通知」用。
 *
 * 昵称取不到不算错（不同框架/不同适配器挂法不一样），返回空串即可 ——
 * 这个列表是给人看的，有 id 就够选了。
 *
 * @returns {{id: string, name: string}[]}
 */
export function listBots() {
  const out = []
  for (const id of botIds()) {
    const b = resolveBot(id)
    const name = b?.nickname || b?.info?.nickname || b?.name || ""
    out.push({ id: String(id), name: String(name || "") })
  }
  return out
}

/** 取 uin 列表。`Bot.uin` 各家都是特化数组，展开成普通数组 */
function uinList() {
  try {
    return [...(Bot?.uin || [])].map(String).filter(Boolean)
  } catch {
    return []
  }
}

/** 主人（收通知的人）的号：先读框架配置，退化到「唯一在线账号」 */
async function masterIds() {
  try {
    const cfg = (await import("../../lib/config/config.js")).default
    const keys = Object.keys(cfg?.master || {}).filter(Boolean)
    if (keys.length) return keys
  } catch {
    /* 取不到配置就往下退 */
  }
  return uinList().slice(0, 1)
}

/**
 * 用**指定的那个 bot** 给某人发消息。
 *
 * 为什么要它：`Bot.sendMasterMsg` 是**广播** —— 多个 bot 在线时人手一条，
 * 主人会同时收到好几份同样的过码通知（实测就是这样）。
 *
 * 能力探测三级兜底（各家挂法不同，不能假设某个一定在）：
 *   ① `Bot[botId].pickFriend(target).sendMsg`   —— 最直接
 *   ② `Bot.pickFriend(target, botId)`           —— 部分框架的第二个参数是 self_id
 *   ③ `Bot.sendFriendMsg(botId, target, msg)`   —— TRSS 形态
 *
 * @returns {Promise<boolean>} 是否发成功（失败不抛，交给调用方回落）
 */
async function sendFromBot(botId, targetId, msg) {
  const id = String(botId || "").trim()
  const target = Number(targetId)
  if (!id || !Number.isFinite(target)) return false

  const bot = resolveBot(id)

  if (bot && typeof bot.pickFriend === "function") {
    try {
      const friend = bot.pickFriend(target)
      if (typeof friend?.sendMsg === "function") {
        await friend.sendMsg(msg)
        return true
      }
    } catch (err) {
      logMsg("debug", `用 bot ${id} 发消息失败（pickFriend on bot）：${err?.message || err}`)
    }
  }

  if (typeof Bot?.pickFriend === "function") {
    try {
      const friend = Bot.pickFriend(target, id)
      if (typeof friend?.sendMsg === "function") {
        await friend.sendMsg(msg)
        return true
      }
    } catch (err) {
      logMsg("debug", `用 bot ${id} 发消息失败（Bot.pickFriend 带 self_id）：${err?.message || err}`)
    }
  }

  if (typeof Bot?.sendFriendMsg === "function") {
    try {
      await Bot.sendFriendMsg(id, target, msg)
      return true
    } catch (err) {
      logMsg("debug", `用 bot ${id} 发消息失败（sendFriendMsg）：${err?.message || err}`)
    }
  }

  return false
}

/**
 * 给主人发消息。按可用性依次尝试：
 *
 *   0. **配置了 `notifyBot`** → 只用那一个 bot 发（避免多个 bot 各发一份）
 *   1. `Bot.sendMasterMsg`  —— TRSS / JiuLi 都有，最省事
 *   2. 直接给 cfg.master 里的号发好友消息
 *   3. 都不可用就只写日志，绝不因为「通知发不出去」把过码流程带崩
 *
 * 导出是为了**可行为测试**（`test_notify.mjs` 直接调它，而不是 grep 源码）——
 * 上次接口契约的坑就是「只做字符串检查，同文件别的行恰好也让字符串出现」。
 *
 * @param {string} msg
 * @param {object} [cfg] 当前配置（含 notifyBot）
 */
export async function notify(msg, cfg) {
  const want = String(cfg?.notifyBot || "").trim()

  // ① 指定了通知 bot：只走它，发不出去才回落（回落是**故意的** ——
  //    宁可多发一份，也不能让主人错过验证链接）
  if (want) {
    const targets = await masterIds()
    for (const t of targets) {
      if (await sendFromBot(want, t, msg)) return
    }
    logMsg("warn", `指定的通知 bot ${want} 发不出去，改用默认通道`)
  }

  if (typeof Bot?.sendMasterMsg === "function") {
    try {
      await Bot.sendMasterMsg(msg)
      return
    } catch (err) {
      logMsg("debug", `sendMasterMsg 失败，改用备用通道：${err?.message || err}`)
    }
  }

  const targets = await masterIds()
  for (const id of targets) {
    try {
      if (typeof Bot?.pickFriend === "function") {
        const friend = Bot.pickFriend(Number(id))
        if (typeof friend?.sendMsg === "function") {
          await friend.sendMsg(msg)
          return
        }
      }
      if (typeof Bot?.sendFriendMsg === "function") {
        await Bot.sendFriendMsg(Bot.uin, Number(id), msg)
        return
      }
    } catch (err) {
      logMsg("debug", `给 ${id} 发消息失败：${err?.message || err}`)
    }
  }
  logMsg("info", `（通知未送达，仅记录）${msg}`)
}

/** 服务地址：配置里的 host/port */
export function baseUrlOf(cfg) {
  const host = cfg?.host || "127.0.0.1"
  const port = cfg?.port || 8767
  return `http://${host}:${port}`
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

  const uins = uinList()
  if (uins.length === 1) return uins[0]

  const ids = botIds()
  if (ids.length === 1) return ids[0]
  // 多个账号时退而求其次：优先挑 URL 里出现过的那一个
  const hit = uins.find(id => url.includes(id)) || ids.find(id => url.includes(id))
  return hit ? String(hit) : ""
}

/**
 * 把 ticket 交给适配器提交。
 *
 * 优先走 `verify.<QQ>`：ICQQ-Plugin 的默认分支拿到 msg 就直接
 * `bot.submitSlider(msg)`，这样它的等待循环会正常结束、不会 3 分钟后
 * 再报一句「滑动验证超时」。
 * 没有监听者时直接调 `submitSlider`（JiuLi / 其它框架上更保险）。
 *
 * @param {string} id     登录中的 QQ 号
 * @param {string} ticket
 * @param {object} [cfg]  当前配置（转发 `reply` 时要带上，通知 bot 的选择在里面）
 * @returns {Promise<"verify"|"direct">} 实际走的通道
 */
async function submitTicket(id, ticket, cfg) {
  const event = `verify.${id}`
  const hasListener = typeof Bot?.listenerCount === "function" && Bot.listenerCount(event) > 0
  if (hasListener) {
    // `em` 是云崽给 verify 通道的特化方法（TRSS `lib/bot.js:426`、JiuLi `lib/core/bot.js:581`），
    // 行为等价于 `emit` 但会走框架自己的日志；fork 上万一没有就退回原生 emit。
    const payload = { msg: ticket, reply: m => notify(m, cfg) }
    if (typeof Bot.em === "function") Bot.em(event, payload)
    else if (typeof Bot.emit === "function") Bot.emit(event, payload)
    else throw new Error("Bot 既没有 em() 也没有 emit()，无法提交 ticket")
    return "verify"
  }

  const bot = Bot?.[id]
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

  const handler = async data => {
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
        await notify(`收到滑块验证，请手动完成：\n${url}`, cfg)
        return
      }

      logMsg("info", `收到滑块验证（uin=${id || "未知"}），开始自动过码`)
      await notify(`[${id || "?"}] 正在自动过码，请稍候…`, cfg)

      const ticket = await solveSlider({
        url,
        uin: id,
        baseUrl,
        port: cfg.port,
        timeout: cfg.timeout,
        logger: { debug: m => logMsg("debug", m) },
      })
      if (!id) throw new Error("认不出是哪个 QQ 在登录，无法提交 ticket")

      const via = await submitTicket(id, ticket, cfg)
      logMsg("info", `自动过码成功（${via}）ticket=${String(ticket).slice(0, 16)}…`)
      await notify(`[${id}] 自动过码成功`, cfg)
    } catch (err) {
      logMsg("error", `自动过码失败：${err?.stack || err}`)
      await notify(
        `[${id || "?"}] 自动过码失败：${err?.message || err}\n` +
          `可手动完成验证：\n${url}`,
        cfg,
      )
    } finally {
      inFlight.delete(url)
    }
  }

  if (typeof Bot?.on !== "function") {
    logMsg("error", "Bot 上找不到 on()，无法接管滑块验证")
    return false
  }
  Bot.on("system.login.slider", handler)

  logMsg("info", "已接管滑块验证（system.login.slider）")
  return true
}

/** 汇总当前状态，给 #滑块过码状态 用 */export async function getStatus(cfg) {
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
    running: !!running,
    health: running,
    env: envReport(),
  }
}
