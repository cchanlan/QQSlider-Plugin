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

import { fileURLToPath, pathToFileURL } from "node:url"
import path from "node:path"

import { health, depsReady, envReport } from "./service.js"
import { solveSlider } from "./solver.js"
// `findRoot()` 从插件目录往上找第一个带 `lib/plugins` 的目录 —— 就是框架根。
// **别自己拼相对路径**：本文件在 `<root>/plugins/QQSlider-Plugin/utils/`，
// 到 `<root>/lib/config/config.js` 要往上三级，少一级就永远 import 失败
// （这正是 2026-10-09「指定推送没效果」的根因之一）。
import { findRoot } from "./config.js"

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

/**
 * 无副作用地读框架的「主人配置」。
 *
 * ## 为什么不直接 `import(config.js)`
 *
 * 框架那个模块是个**带副作用的单例**：构造函数里就去
 * `process.cwd()/config/default_config/` 读默认配置。而 cwd 不一定是框架根 ——
 * 实测在 `E:\Yunzai` 下直接跑会：
 *
 *     Error: ENOENT: no such file or directory,
 *            scandir 'E:\Yunzai\plugins\QQSlider-Plugin\config\default_config\'
 *
 * 于是整个 `masterIds()` 崩掉、回落到广播 —— **「指定主人」又白做了**。
 * 所以这里自己读文件：`config/config/other.yaml`（或 `.json`）。
 *
 * 兼容三种落法（不同框架版本/用户手改都可能）：
 *   · `config/config/other.yaml`   —— 标准
 *   · `config/other.yaml`          —— 老一点的位置
 *   · 各自的 `.json` 版本
 *
 * @returns {Promise<object>} 配置对象；读不到返回 {}
 */
async function readFrameworkOther(root) {
  const fsMod = await import("node:fs")
  const fs = fsMod?.default || fsMod
  const { parse: parseYaml } = await loadYamlSafe()

  const candidates = [
    path.join(root, "config", "config", "other.yaml"),
    path.join(root, "config", "config", "other.yml"),
    path.join(root, "config", "other.yaml"),
    path.join(root, "config", "other.yml"),
    path.join(root, "config", "config", "other.json"),
    path.join(root, "config", "other.json"),
  ]

  for (const f of candidates) {
    try {
      if (!fs.existsSync(f)) continue
      const raw = fs.readFileSync(f, "utf8")
      const data = f.endsWith(".json") ? JSON.parse(raw) : parseYaml(raw)
      if (data && typeof data === "object") return data
    } catch (err) {
      logMsg("debug", `读 ${f} 失败：${err?.message || err}`)
    }
  }
  return {}
}

/** 拿宿主的 yaml 模块（`yaml` 优先，退 `js-yaml`）；都拿不到返回一个兜底 parse */
async function loadYamlSafe() {
  for (const spec of ["yaml", "js-yaml"]) {
    try {
      const mod = await import(spec)
      const lib = mod?.default || mod
      if (typeof lib?.parse === "function") return { parse: lib.parse }
      if (typeof lib?.load === "function") return { parse: lib.load }
    } catch {
      /* 试下一个 */
    }
  }
  return { parse: parseYamlLite }
}

/**
 * 极简 YAML 解析兜底 —— 只支持**我们真正需要的形状**。
 *
 * ## 什么时候会用到它
 *
 * 宿主没装 `yaml` / `js-yaml` 时。**这在真实环境是会发生的**：
 * 插件不在框架的 `node_modules` 解析路径上（装在别处、或用了软链），
 * `import("yaml")` 就会失败。实测把插件目录复制到临时目录跑，
 * 就必然走这条路。
 *
 * ## 为什么第一版是错的（教训）
 *
 * 第一版只认 `key: value` 一行式，于是标准的多行列表
 *
 *     master:
 *       - '3942704893:2606138772'
 *
 * 里 `master:` 的值为空被**整行跳过**，后面的 `- ...` 又不匹配 key 正则，
 * 结果读出**空配置** → 拿不到主人号 → 悄悄退回广播。
 * 「指定主人」看起来就还是没效果 —— 一个兜底函数写弱了，
 * 把上面的功能整个废掉，而且**没有任何报错**。
 *
 * 所以这里把「`key:` 后面跟一串 `- item`」这种最常见的形状补上。
 */
function parseYamlLite(text) {
  const out = {}
  let curKey = null   // 正在收集的列表对应的 key

  const unquote = s => String(s).trim().replace(/^["']|["']$/g, "")

  for (const rawLine of String(text || "").split(/\r?\n/)) {
    // 丢掉注释和空行（空行不清 curKey —— YAML 列表项之间允许空行）
    const line = rawLine.replace(/\s+#.*$/, "")
    if (!line.trim()) continue

    const indent = line.length - line.trimStart().length
    const t = line.trim()

    // 列表项：`- xxx`（缩进比 key 深）
    if (t.startsWith("- ")) {
      const item = unquote(t.slice(2))
      if (curKey && item) {
        if (!Array.isArray(out[curKey])) out[curKey] = []
        out[curKey].push(item)
      }
      continue
    }

    // key: value / key:
    const m = /^([A-Za-z0-9_]+)\s*:\s*(.*)$/.exec(t)
    if (!m) continue
    const [, k, vRaw] = m
    const v = vRaw.trim()
    if (!v) {
      // 空值 → 可能是「后面跟列表」，先占位，等 `- ` 行来填
      curKey = indent === 0 ? k : curKey
      if (indent === 0 && !(k in out)) out[k] = []
      continue
    }
    curKey = null
    if (v === "[]") out[k] = []
    else if (v.startsWith("[") && v.endsWith("]")) {
      out[k] = v.slice(1, -1).split(",").map(unquote).filter(Boolean)
    } else out[k] = unquote(v)
  }
  return out
}

/**
 * 读框架配置里的主人号 —— **收通知的人**。
 *
 * ## ⚠️ 两个曾经踩死的坑（2026-10-09 实测「指定 bot 推送没效果」的根因）
 *
 * **坑① 相对路径少了一级。** 本文件在 `<root>/plugins/QQSlider-Plugin/utils/`，
 * 而框架配置在 `<root>/lib/config/config.js` —— 要往上**三级**：
 *
 *     utils/ → QQSlider-Plugin/ → plugins/ → <root>/lib/config/config.js
 *     "../../lib/..."    → <root>/plugins/lib/...   ❌ 不存在
 *     "../../../lib/..." → <root>/lib/...           ✓
 *
 * 路径错了就永远 import 失败 → 掉进兜底 → 拿不到主人号。
 *
 * **坑② `cfg.master` 的 key 是「bot 号」不是「主人号」。** TRSS 的
 * `config.js` 里那段原文：
 *
 *     get master() { ... for (i of master) { i = i.split(":")
 *       const bot_id = i.shift();  const user_id = i.join(":")
 *       masters[bot_id] = [user_id] } return masters }
 *     get uin() { return Object.keys(this.master) }      // ← 这里也是 bot 号
 *
 * 所以在配置里写 `master: ["123:456"]`（123=bot、456=主人）时，
 * `Object.keys(cfg.master)` 拿到的是 **"123"（bot 自己）**。
 * 直接拿它当收件人 = **把通知发给 bot 自己**，主人永远收不到。
 * **要取的是 value（`master[bot]` 里的数组），那才是主人号。**
 *
 * ## 取号的优先级
 *
 *   ① 插件配置里的 `notifyMaster`（用户显式指定，最高优先）
 *   ② 框架配置 `cfg.master` 的 **value 展开**（主人号）
 *   ③ `cfg.masterQQ`（有些版本单独提供，是个数组）
 *   ④ 兜底：唯一在线账号（只有他一个 bot 时，通常就是自己给自己发）
 *
 * @param {object} [cfg] 插件配置（含 notifyMaster）
 * @returns {Promise<string[]>}
 */
export async function masterIds(cfg) {
  /** @type {string[]} */
  const out = []

  const push = v => {
    const s = String(v ?? "").trim()
    if (s && !out.includes(s)) out.push(s)
  }

  // ① 插件里**显式指定**了收件人 → 就只发给他，不再叠加框架里的其它主人。
  //    （指定了还发给别人，等于指定没用 —— 这正是「没效果」的观感来源。）
  const want = String(cfg?.notifyMaster || "").trim()
  if (want) push(want)

  // ②③ 框架配置 —— **只在没显式指定时**才用，避免「指定了还发给别人」
  //
  // ⚠️ **不要 `import` 框架的 `lib/config/config.js`**。它是个带副作用的
  //    单例：构造函数里去 `cwd/config/default_config/` 读默认配置，
  //    而且 `process.cwd()` 不一定是框架根 —— 从别处调会直接
  //    `ENOENT: scandir .../config/default_config/` 抛出来（实测在
  //    `E:\Yunzai` 下直接跑就会崩），于是又掉回广播兜底。
  //    所以这里**自己读 YAML/JSON 文件**，无副作用、稳。
  if (!want) {
    const root = findRoot()
    if (root) {
      try {
        const raw = await readFrameworkOther(root)
        // ★ 取 value 展开 —— key 是 bot 号，value 才是主人号（见上面坑②）
        //
        // ⚠️ `master` 在配置文件里是**扁平列表** `["bot号:主人号", ...]`，
        //    框架的 `get master()` 才把它拆成 `{bot: [主人]}`。
        //    所以我们自己读文件时**两种形状都要认**：
        //      · 对象 `{ "bot": ["master"] }` → 取 value
        //      · 列表 `["bot:master"]`        → 取 `:` 后面那段
        const m = raw?.master
        if (Array.isArray(m)) {
          for (const item of m) {
            const s = String(item ?? "").trim()
            if (!s) continue
            // "bot:master" / "bot:master1,master2" 都取冒号后的部分
            const idx = s.indexOf(":")
            if (idx >= 0) {
              s.slice(idx + 1).split(/[,，、\s]+/).forEach(push)
            }
            // 没有冒号就认不出谁是主人，跳过（不能瞎猜成 bot 号）
          }
        } else if (m && typeof m === "object") {
          for (const v of Object.values(m)) {
            if (Array.isArray(v)) v.forEach(push)
            else push(v)
          }
        }
        // 有的版本单独给一个 masterQQ 数组
        const mq = raw?.masterQQ || raw?.masterQQs
        if (Array.isArray(mq)) mq.forEach(push)
        else if (mq) push(mq)
      } catch (err) {
        logMsg("debug", `读框架主人配置失败（改用兜底）：${err?.message || err}`)
      }
    }
  }

  if (out.length) return out

  // ④ 兜底：只有一个在线账号时就发给它
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
 * 给主人发消息。
 *
 * ## 为什么要能「指定主人」（2026-10-09 主人要求）
 *
 * 框架自带的 `Bot.sendMasterMsg` 是**广播**：它遍历所有在线 bot，
 * 每个 bot 都给自己的主人发一遍 —— 所以主人有 4 个号在跑时，
 * 一条过码通知会**收到 4 份**（截图里风间菜菜 / HLbot / HL喵喵 各发一条）。
 *
 * 一开始做的是「指定用哪个 bot 发」，但**实测没效果**，根因有两个（见 `masterIds`）：
 *   ① 读框架配置的相对路径少了一级 → 永远 import 失败
 *   ② `cfg.master` 的 key 是 **bot 号**、value 才是主人号，取反了 → 发给 bot 自己
 *
 * 改成「指定主人」之后语义清楚了：**只发给这一个号**，
 * 由谁发不重要（谁在线谁发，发不出去依次换）。
 *
 * ## 发送顺序
 *
 *   ① 显式指定的 `notifyMaster`（或框架配的主人号）→ 逐个试
 *   ② 说不清给谁时，才退回 `Bot.sendMasterMsg`（广播，可能多份）
 *   ③ 都不行就只写日志，**绝不因为通知发不出去把过码流程带崩**
 *
 * 导出是为了**可行为测试**（`test_notify.mjs` 直接调它，而不是 grep 源码）。
 *
 * @param {string} msg
 * @param {object} [cfg] 当前配置（含 notifyMaster）
 */
export async function notify(msg, cfg) {
  const targets = await masterIds(cfg)

  // ① 有明确收件人 → **每个不同的主人都通知到**（`masterIds` 已去重，
  //    所以「一个人有 4 个 bot」这种情况只会发 1 条，不会 4 条）。
  //
  //    「由谁发」不重要（谁在线谁发），只要发得出去就行 —— 依次试每个在线 bot。
  //    `notifyBot` 是**内部**的可选覆盖项（默认空）：一般不用填，
  //    主人只需关心「发给谁」（`notifyMaster`）。
  if (targets.length) {
    const explicitBot = String(cfg?.notifyBot || "").trim()
    const senders = explicitBot ? [explicitBot, ...botIds()] : botIds()
    let delivered = 0
    for (const t of targets) {
      let ok = false
      for (const botId of senders) {
        if (await sendFromBot(botId, t, msg)) {
          ok = true
          break
        }
      }
      // bot 通道都不行 → 试框架的通用通道（限定收件人，不是广播）
      if (!ok) {
        try {
          if (typeof Bot?.pickFriend === "function") {
            const friend = Bot.pickFriend(Number(t))
            if (typeof friend?.sendMsg === "function") {
              await friend.sendMsg(msg)
              ok = true
            }
          }
        } catch (err) {
          logMsg("debug", `给 ${t} 发消息失败：${err?.message || err}`)
        }
      }
      if (ok) delivered++
    }
    if (delivered === targets.length) return
    logMsg("warn", `部分主人号没发出去（成功 ${delivered}/${targets.length}），`
                   + "改用默认通道兜底")
  }

  // ② 说不清收件人 —— 退回广播（宁可多发一份，也不能让主人错过验证链接）
  if (typeof Bot?.sendMasterMsg === "function") {
    try {
      await Bot.sendMasterMsg(msg)
      return
    } catch (err) {
      logMsg("debug", `sendMasterMsg 失败：${err?.message || err}`)
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
