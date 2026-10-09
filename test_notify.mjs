/**
 * 通知 bot 选择的自测 —— 不联网、不启云崽，mock 一个 `Bot` 就能跑。
 *
 * 跑法（在插件根目录）：
 *   node test_notify.mjs
 *
 * 钉住的核心行为：
 *   · 不指定 notifyBot → 走框架默认（多 bot 时各发一份，原行为）
 *   · 指定 notifyBot   → **只有那一个 bot 发**
 *   · 指定的 bot 发不出去 → 回落默认通道（宁可多发，不能让主人错过链接）
 *   · 昵称取不到 / 没有 bot 列表也不能崩
 *
 * ⚠️ 这里**直接调 `notify()` 观察真实发送记录**，不做源码字符串检查 ——
 * 上一轮接口契约的教训：字符串检查会被同文件里别的行误判成通过。
 */

const sent = []
const makeBot = (id, nickname, { fail = false } = {}) => ({
  uin: String(id),
  nickname,
  pickFriend(target) {
    return {
      async sendMsg(msg) {
        if (fail) throw new Error(`bot ${id} 发送失败`)
        sent.push({ from: String(id), to: String(target), msg: String(msg) })
      },
    }
  },
})

const bots = {
  111111: makeBot(111111, "风间菜菜"),
  222222: makeBot(222222, "HL喵喵-测试中"),
  333333: makeBot(333333, "坏掉的", { fail: true }),
}

let broadcastCount = 0
globalThis.Bot = {
  bots,
  uin: ["111111", "222222", "333333"],
  listenerCount: () => 0,
  // 广播通道：模拟 sendMasterMsg 会**发给所有 bot**
  async sendMasterMsg(msg) {
    broadcastCount++
    for (const id of Object.keys(bots)) {
      sent.push({ from: String(id), to: "master", broadcast: true, msg: String(msg) })
    }
  },
}

globalThis.logger = { info() {}, warn() {}, debug() {}, error() {} }

let failed = 0
const check = (name, ok, detail = "") => {
  console.log(`  [${ok ? "OK  " : "FAIL"}] ${name}${detail ? `   ${detail}` : ""}`)
  if (!ok) failed++
}

const hook = await import("./utils/hook.js")

// ══════════════════════════════════════════════════════════════════
console.log("=".repeat(74))
console.log("① listBots：列出可用 bot")
console.log("=".repeat(74))
const list = hook.listBots()
check("能列出 3 个 bot", list.length === 3, list.map(b => b.id).join(","))
check("带出了昵称", list.some(b => b.name === "风间菜菜"),
      list.map(b => b.name).join("/"))

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(74))
console.log("② 不指定 notifyBot → 走框架默认（广播）")
console.log("=".repeat(74))
sent.length = 0; broadcastCount = 0
// 没有 master 配置可取（测试环境 import 会失败）→ masterIds 退化成 uinList()[0]
await hook.notify("测试消息-A", { notifyBot: "" })
check("走了广播通道", broadcastCount === 1, `broadcastCount=${broadcastCount}`)
check("广播会发给所有 bot（这就是主人收到多份的原因）",
      sent.length === 3, `发了 ${sent.length} 条`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(74))
console.log("③ ★ 指定 notifyBot → 只有那一个 bot 发")
console.log("=".repeat(74))
for (const target of ["111111", "222222"]) {
  sent.length = 0; broadcastCount = 0
  await hook.notify(`测试消息-${target}`, { notifyBot: target })
  check(`指定 ${target}：只发了 1 条`, sent.length === 1, `实发 ${sent.length}`)
  check(`指定 ${target}：来源就是它`, sent[0]?.from === target, `实为 ${sent[0]?.from}`)
  check(`指定 ${target}：没走广播`, broadcastCount === 0)
  check(`指定 ${target}：内容是原文`, sent[0]?.msg === `测试消息-${target}`)
}

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(74))
console.log("④ 指定的 bot 发不出去 → 回落默认通道（不能丢通知）")
console.log("=".repeat(74))
sent.length = 0; broadcastCount = 0
await hook.notify("测试消息-坏bot", { notifyBot: "333333" })
check("坏 bot 发失败后走了广播", broadcastCount === 1, `broadcastCount=${broadcastCount}`)
check("最终仍然有人收到", sent.length > 0, `发了 ${sent.length} 条`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(74))
console.log("⑤ 指定一个不存在的 bot（离线）→ 也不能崩，且要回落")
console.log("=".repeat(74))
sent.length = 0; broadcastCount = 0
await hook.notify("测试消息-不存在", { notifyBot: "999999" })
check("没抛异常且回落广播", broadcastCount === 1, `broadcastCount=${broadcastCount}`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(74))
console.log("⑥ 边界：昵称取不到 / cfg 缺失 / 没有 bots")
console.log("=".repeat(74))

// 昵称取不到
globalThis.Bot.bots = { 444444: { uin: "444444" } }   // 没有 nickname、没有 pickFriend
sent.length = 0; broadcastCount = 0
const l2 = hook.listBots()
check("没有昵称也能列出来", l2.length === 1 && l2[0].id === "444444",
      JSON.stringify(l2))
await hook.notify("测试消息-无昵称", { notifyBot: "444444" })
check("没有 pickFriend 时回落广播", broadcastCount === 1)

// cfg 缺失
sent.length = 0; broadcastCount = 0
await hook.notify("测试消息-无cfg")
check("不传 cfg 也不崩（走广播）", broadcastCount === 1)

// 没有 bots
globalThis.Bot.bots = {}
sent.length = 0; broadcastCount = 0
const l3 = hook.listBots()
check("没有 bot 时列表为空数组", Array.isArray(l3) && l3.length === 0)
await hook.notify("测试消息-无bot", { notifyBot: "111111" })
check("没有 bot 时不抛异常", true)

// 连 sendMasterMsg 都没有
globalThis.Bot = { bots: {}, uin: [] }
broadcastCount = 0
await hook.notify("测试消息-什么都没有", { notifyBot: "" })
check("连广播通道都没有时也不抛异常", true)

console.log()
console.log("=".repeat(74))
console.log(failed ? `★ ${failed} 项失败` : "全部通过 ✅")
console.log("=".repeat(74))
process.exit(failed ? 1 : 0)
