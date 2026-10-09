/**
 * 过码通知「发给谁」的自测 —— mock 一个 `Bot` + 一个假框架配置就能跑。
 *
 * 跑法（在插件根目录）：node test_notify.mjs
 *
 * ## 这个测试是为哪次事故写的
 *
 * 主人反馈「指定 bot 推送**没效果**：4 个 bot 各发一条」。
 * 挖出来**两个叠加的 bug**，都是这个测试现在钉住的：
 *
 *   ① **相对路径少了一级** —— `import("../../lib/config/config.js")`
 *      从 `<root>/plugins/QQSlider-Plugin/utils/` 出发只到 `<root>/plugins/lib/...`，
 *      而真实配置在 `<root>/lib/config/config.js` → 永远 import 失败 → 拿不到主人号。
 *   ② **`cfg.master` 的 key 是「bot 号」、value 才是「主人号」**
 *      （TRSS 里 `get uin() { return Object.keys(this.master) }` 拿的就是 bot 号）
 *      → 取 `Object.keys()` 等于**把通知发给 bot 自己**，主人收不到。
 *
 * 所以测试用**真实形状**的框架配置（`master: {"bot号": ["主人号"]}`），
 * 并断言「只发给了主人号」而不是 bot 号。
 */

const sent = []
const broadcasts = []

// ── 真实形状的框架配置：key=bot 号，value=[主人号] ────────────────
globalThis.__FAKE_MASTER__ = {
  111111: ["999999"],      // bot 111111 的主人
  222222: ["999999"],      // bot 222222 的主人（同一个主人）
  333333: ["888888"],      // bot 333333 的主人是另一个
}

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
  222222: makeBot(222222, "HLbot"),
  333333: makeBot(333333, "坏掉的", { fail: true }),
}

globalThis.Bot = {
  bots,
  uin: ["111111", "222222", "333333"],
  listenerCount: () => 0,
  async sendMasterMsg(msg) {
    // 真实行为：遍历所有 bot，每个都发一份
    for (const id of Object.keys(bots)) {
      broadcasts.push(String(id))
      sent.push({ from: String(id), to: "master", broadcast: true, msg: String(msg) })
    }
  },
}

globalThis.logger = { info() {}, warn() {}, debug() {}, error() {} }

// ── mock 框架根：让 findRoot() 能找到带 lib/plugins 的目录 ────────
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { pathToFileURL } from "node:url"

const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "qqslider-fake-root-"))
fs.mkdirSync(path.join(ROOT, "lib", "plugins"), { recursive: true })
fs.mkdirSync(path.join(ROOT, "config", "config"), { recursive: true })

// ★ 用**真实形状**的 other.yaml：`master` 是扁平列表 `["bot号:主人号"]`
//   （框架的 `get master()` 才会把它拆成 {bot: [主人]}）
//   这里故意**不装 yaml 模块**，正好也测了自带的极简解析兜底。
fs.writeFileSync(
  path.join(ROOT, "config", "config", "other.yaml"),
  [
    "master:",
    "  - '111111:999999'",
    "  - '222222:999999'",
    "  - '333333:888888'",
    "masterQQ:",
    "  - '999999'",
    "",
  ].join("\n"),
  "utf8",
)
// 注意：**故意不放 lib/config/config.js** —— 真实框架那个模块带副作用
// （构造时读 cwd/config/default_config/，cwd 不对就 ENOENT 崩掉），
// 我们的实现压根不该 import 它。放个会抛异常的假文件来钉住这点。
fs.mkdirSync(path.join(ROOT, "lib", "config"), { recursive: true })
fs.writeFileSync(
  path.join(ROOT, "lib", "config", "config.js"),
  "throw new Error('不应 import 框架 config.js（它带副作用、会因 cwd 崩）')\n",
  "utf8",
)

let failed = 0
const check = (name, ok, detail = "") => {
  console.log(`  [${ok ? "OK  " : "FAIL"}] ${name}${detail ? `   ${detail}` : ""}`)
  if (!ok) failed++
}

// ══════════════════════════════════════════════════════════════════
// 让被测模块以为自己在那个假框架下：把 hook.js 里的 findRoot 指向 ROOT
// 最省事的做法：真的复制一份插件目录结构到 ROOT 下再 import。
// ══════════════════════════════════════════════════════════════════
const PLUGIN_SRC = path.resolve("utils")
const PLUGIN_DST = path.join(ROOT, "plugins", "QQSlider-Plugin")
fs.mkdirSync(PLUGIN_DST, { recursive: true })
for (const f of fs.readdirSync(PLUGIN_SRC)) {
  if (f.endsWith(".js")) {
    fs.copyFileSync(path.join(PLUGIN_SRC, f), path.join(PLUGIN_DST, f))
  }
}

const hook = await import(pathToFileURL(path.join(PLUGIN_DST, "hook.js")).href)

// ══════════════════════════════════════════════════════════════════
console.log("=".repeat(76))
console.log("① masterIds：必须取到「主人号」，不是 bot 号")
console.log("=".repeat(76))
const ids = await hook.masterIds({})
console.log(`        读到的主人号 = ${JSON.stringify(ids)}`)
check("读到了主人号 999999", ids.includes("999999"), JSON.stringify(ids))
check("读到了主人号 888888", ids.includes("888888"), JSON.stringify(ids))
check("★ 没有把 bot 号当主人号（坑②）",
      !ids.includes("111111") && !ids.includes("222222") && !ids.includes("333333"),
      JSON.stringify(ids))

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("② 显式指定 notifyMaster → 只发给那一个号")
console.log("=".repeat(76))
sent.length = 0; broadcasts.length = 0
await hook.notify("测试-A", { notifyMaster: "999999" })
const tos = [...new Set(sent.map(s => s.to))]
console.log(`        sent = ${JSON.stringify(sent.map(s => ({ from: s.from, to: s.to })))}`)
check("只发给了 999999", tos.length === 1 && tos[0] === "999999", JSON.stringify(tos))
check("只发了 1 条（不是 4 个 bot 各一条）", sent.length === 1, `实发 ${sent.length}`)
check("没走广播", broadcasts.length === 0, `broadcast=${broadcasts.length}`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("③ 指定另一个主人号 → 只发给他")
console.log("=".repeat(76))
sent.length = 0; broadcasts.length = 0
await hook.notify("测试-B", { notifyMaster: "888888" })
const tos2 = [...new Set(sent.map(s => s.to))]
check("只发给了 888888", tos2.length === 1 && tos2[0] === "888888", JSON.stringify(tos2))
check("只发了 1 条", sent.length === 1, `实发 ${sent.length}`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("④ 不指定 → 用框架配置里的主人（仍然不是「每个 bot 一条」）")
console.log("=".repeat(76))
sent.length = 0; broadcasts.length = 0
await hook.notify("测试-C", {})
const tos3 = [...new Set(sent.map(s => s.to))]
console.log(`        收件人 = ${JSON.stringify(tos3)}  条数=${sent.length}`)
check("收件人是主人号（999999/888888），不是 bot 号",
      tos3.every(t => !["111111", "222222", "333333"].includes(t)),
      JSON.stringify(tos3))
check("没走广播", broadcasts.length === 0, `broadcast=${broadcasts.length}`)
// 两个主人号各 1 条 = 2 条（不是「3 个 bot × 各一份」）
check("条数 = 主人号个数（2），不是 bot 个数（3）", sent.length === 2, `实发 ${sent.length}`)

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("⑤ 指定一个发不出去的 bot → 换别的 bot 发，别丢通知")
console.log("=".repeat(76))
sent.length = 0; broadcasts.length = 0
await hook.notify("测试-D", { notifyMaster: "999999", notifyBot: "333333" })
check("坏 bot 优先但失败后换人发了", sent.length >= 1, `实发 ${sent.length}`)
check("仍然只发给 999999",
      sent.every(s => s.to === "999999"), JSON.stringify(sent.map(s => s.to)))

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("⑥ 框架配置读不到 → 才退回广播（宁可多发，不能丢）")
console.log("=".repeat(76))
sent.length = 0; broadcasts.length = 0
// 换一个「没有 lib/plugins」的假模块，让 masterIds 读不到配置
const bare = await import(pathToFileURL(path.join(PLUGIN_DST, "hook.js")).href)
const savedBots = globalThis.Bot.bots
globalThis.Bot.bots = {}          // 也没有在线 bot
try {
  await bare.notify("测试-E", {})
  check("没有收件人时退回广播（没抛异常）", broadcasts.length >= 1,
        `broadcast=${broadcasts.length}`)
} catch (e) {
  check("没有收件人时退回广播（没抛异常）", false, String(e))
} finally {
  globalThis.Bot.bots = savedBots
}

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("⑦ index.js：配置项与指令名")
console.log("=".repeat(76))
const idx = fs.readFileSync("index.js", "utf8")
check("默认配置含 notifyMaster", idx.includes("notifyMaster:"))
check("指令方法叫 NotifyMaster", idx.includes("async NotifyMaster()"))
check("已无 notifyBot 残留", !idx.includes("notifyBot"))
check("状态里显示通知对象", idx.includes("通知："))

// ══════════════════════════════════════════════════════════════════
console.log()
console.log("=".repeat(76))
console.log("⑧ 兜底 YAML 解析（宿主没装 yaml 模块时用）")
console.log("=".repeat(76))
// 直接把兜底解析函数抠出来单测 —— 它写弱了会让「指定主人」静默失效
const hookSrc = fs.readFileSync(path.join(PLUGIN_DST, "hook.js"), "utf8")
const liteMatch = /function parseYamlLite\(text\) \{([\s\S]*?)\n\}/.exec(hookSrc)
check("能定位到 parseYamlLite", !!liteMatch)
if (liteMatch) {
  // eslint-disable-next-line no-new-func
  const parseLite = new Function("text", liteMatch[1])
  const cases = [
    ["标准多行列表", "master:\n  - '111111:999999'\n  - '222222:999999'\n",
     ["111111:999999", "222222:999999"]],
    ["无引号", "master:\n  - 111111:999999\n", ["111111:999999"]],
    ["行内数组", "master: ['111111:999999']\n", ["111111:999999"]],
    ["带注释", "master:\n  - '111111:999999'  # 主人\n", ["111111:999999"]],
    ["列表间空行", "master:\n  - 'a:b'\n\n  - 'c:d'\n", ["a:b", "c:d"]],
    ["同时有多个 key",
     "autoFriend: 0\nmasterQQ:\n  - '999999'\nmaster:\n  - '111111:999999'\n",
     ["111111:999999"]],
  ]
  for (const [name, text, want] of cases) {
    const got = parseLite(text)
    const m = got?.master
    check(`兜底解析 ${name}`, JSON.stringify(m) === JSON.stringify(want),
          `→ ${JSON.stringify(m)}`)
  }
  const mq = parseLite("masterQQ:\n  - '999999'\n")?.masterQQ
  check("兜底解析 masterQQ", JSON.stringify(mq) === JSON.stringify(["999999"]),
        JSON.stringify(mq))
}

console.log()
console.log("=".repeat(76))
console.log(failed ? `★ ${failed} 项失败` : "全部通过 ✅")
console.log("=".repeat(76))

// 清理临时框架
try { fs.rmSync(ROOT, { recursive: true, force: true }) } catch {}
process.exit(failed ? 1 : 0)
