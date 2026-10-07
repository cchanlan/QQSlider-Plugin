/**
 * 过码服务的生命周期管理
 *
 * 负责：找 Python → 建 venv → 装依赖 → 拉起服务 → 健康检查 → 退出时收摊
 *
 * 设计原则：**插件加载时不该阻塞云崽启动**，所以：
 *   · 服务已在跑 → 直接用
 *   · 没在跑 → 后台自动拉起（不 await 太久）
 *   · 依赖没装 → 后台自动装，装完自动起
 * 过码是登录时才用到的功能，启动时慢一点没关系，但绝不能卡住云崽。
 */

import { spawn, spawnSync } from "node:child_process"
import fs from "node:fs"
import fsp from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SERVICE_DIR = path.join(__dirname, "..", "service")
const VENV_DIR = path.join(SERVICE_DIR, ".venv")
const SERVER_PY = path.join(SERVICE_DIR, "server.py")
const REQUIREMENTS = path.join(SERVICE_DIR, "requirements.txt")
const READY_FLAG = path.join(VENV_DIR, ".deps-ok")

/** 子进程句柄（只有我们自己拉起来的才记，别人起的不管） */
let child = null
let starting = null

const isWin = process.platform === "win32"

function venvPython() {
  return isWin
    ? path.join(VENV_DIR, "Scripts", "python.exe")
    : path.join(VENV_DIR, "bin", "python")
}

/**
 * 找一个可用的 Python。
 *
 * 平台差异：Windows 通常只有 `python`（`python3` 往往是 Microsoft Store 的
 * 假别名，执行会报错）；Linux 反过来通常只有 `python3`。
 * 所以两个都试，用 `--version` 验证真的能跑。
 */
export function findPython() {
  const candidates = isWin ? ["python", "py", "python3"] : ["python3", "python"]
  for (const cmd of candidates) {
    try {
      const r = spawnSync(cmd, ["--version"], { encoding: "utf8", timeout: 15000 })
      if (r.status === 0 && /Python\s+3\./.test(r.stdout + r.stderr)) return cmd
    } catch {
      /* 试下一个 */
    }
  }
  return null
}

/** 检查依赖是否已装好（靠标记文件 + 解释器存在双重判断） */
export function depsReady() {
  return fs.existsSync(venvPython()) && fs.existsSync(READY_FLAG)
}

/** 跑一条命令，实时把输出转给 logger */
function run(cmd, args, { cwd = SERVICE_DIR, logger, timeout = 600000 } = {}) {
  return new Promise(resolve => {
    let proc
    try {
      proc = spawn(cmd, args, { cwd, shell: false, windowsHide: true })
    } catch (err) {
      resolve({ code: -1, error: err })
      return
    }
    let out = ""
    const onData = buf => {
      const text = String(buf)
      out += text
      if (logger) for (const line of text.split(/\r?\n/)) if (line.trim()) logger.debug?.(line)
    }
    proc.stdout?.on("data", onData)
    proc.stderr?.on("data", onData)
    const timer = setTimeout(() => {
      try {
        proc.kill()
      } catch {}
      resolve({ code: -1, error: new Error("timeout"), out })
    }, timeout)
    proc.on("error", err => {
      clearTimeout(timer)
      resolve({ code: -1, error: err, out })
    })
    proc.on("close", code => {
      clearTimeout(timer)
      resolve({ code, out })
    })
  })
}

/**
 * 建 venv 并装依赖。**幂等**：已装好就直接返回。
 *
 * 用清华源：默认 PyPI 在国内经常慢到超时（实测直接装会挂住）。
 * 想换源设环境变量 `QQSLIDER_PIP_INDEX`。
 */
export async function installDeps({ logger, force = false } = {}) {
  if (!force && depsReady()) return { ok: true, skipped: true }

  const python = findPython()
  if (!python) {
    return { ok: false, error: "找不到 Python，请先安装 Python 3.9 以上版本" }
  }
  const index = process.env.QQSLIDER_PIP_INDEX || "https://pypi.tuna.tsinghua.edu.cn/simple"

  logger?.info?.(`正在准备 Python 环境（首次约需 1~3 分钟）…`)
  if (!fs.existsSync(venvPython())) {
    logger?.info?.(`创建虚拟环境…`)
    const r = await run(python, ["-m", "venv", VENV_DIR], { logger })
    if (r.code !== 0) {
      return { ok: false, error: `创建虚拟环境失败：${r.error?.message || r.out.slice(-300)}` }
    }
  }

  logger?.info?.(`安装依赖（源：${index}）…`)
  const r = await run(
    venvPython(),
    ["-m", "pip", "install", "--disable-pip-version-check", "-i", index, "-r", REQUIREMENTS],
    { logger },
  )
  if (r.code !== 0) {
    return { ok: false, error: `安装依赖失败：${r.error?.message || r.out.slice(-500)}` }
  }

  // 真验一遍能不能 import，别只看 pip 退出码
  const check = await run(venvPython(), [
    "-c",
    "import cv2,numpy,curl_cffi,requests;print('ok')",
  ])
  if (check.code !== 0 || !check.out.includes("ok")) {
    return { ok: false, error: `依赖校验失败：${check.out.slice(-300)}` }
  }

  await fsp.writeFile(READY_FLAG, new Date().toISOString(), "utf8")
  logger?.info?.(`Python 环境就绪`)
  return { ok: true }
}

/** 健康检查 */
export async function health(baseUrl, timeout = 3000) {
  const base = String(baseUrl || "").replace(/\/+$/, "")
  if (!base) return null
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await fetch(`${base}/health`, { signal: controller.signal })
    return res.ok ? await res.json() : null
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/** 等服务就绪（轮询健康检查） */
async function waitReady(baseUrl, seconds, logger) {
  const deadline = Date.now() + seconds * 1000
  while (Date.now() < deadline) {
    if (await health(baseUrl)) return true
    await new Promise(r => setTimeout(r, 700))
  }
  logger?.warn?.(`过码服务 ${seconds}s 内没就绪`)
  return false
}

/**
 * 确保服务在跑。
 *
 * @returns {Promise<{ok:boolean, started?:boolean, error?:string}>}
 */
export async function ensureRunning({ baseUrl, port, logger, rounds } = {}) {
  if (await health(baseUrl)) return { ok: true, started: false }
  // 并发调用只拉一次
  if (starting) return starting

  starting = (async () => {
    try {
      const deps = await installDeps({ logger })
      if (!deps.ok) return { ok: false, error: deps.error }

      const args = [SERVER_PY, "--port", String(port)]
      if (rounds) args.push("--rounds", String(rounds))
      logger?.info?.(`启动过码服务（端口 ${port}）…`)

      child = spawn(venvPython(), args, {
        cwd: SERVICE_DIR,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      })
      child.stdout?.on("data", b => {
        for (const line of String(b).split(/\r?\n/))
          if (line.trim()) logger?.info?.(`[solver] ${line}`)
      })
      child.stderr?.on("data", b => {
        for (const line of String(b).split(/\r?\n/))
          if (line.trim()) logger?.info?.(`[solver] ${line}`)
      })
      child.on("exit", code => {
        if (code !== 0 && code !== null) logger?.warn?.(`过码服务退出，代码 ${code}`)
        child = null
      })

      // ⚠️ 必须把子进程和它的 stdio 管道都从事件循环里摘掉，
      // 否则云崽会关不掉：`child.unref()` 只摘进程句柄，
      // stdout/stderr 那两根管道是**独立的句柄**，照样把循环 ref 住 ——
      // 表现是 `pm2 stop JiuLi` 卡死、Ctrl+C 退不出去。
      // （实测：只 unref 进程时，脚本结束后 Node 30 秒仍不退出。）
      child.stdout?.unref?.()
      child.stderr?.unref?.()
      child.unref?.()

      const ok = await waitReady(baseUrl, 60, logger)
      return ok ? { ok: true, started: true } : { ok: false, error: "服务启动超时" }
    } catch (err) {
      return { ok: false, error: err?.message || String(err) }
    } finally {
      starting = null
    }
  })()
  return starting
}

/**
 * 停掉我们自己拉起来的服务。
 *
 * ⚠️ 必须等子进程真的退出再返回：`kill()` 是异步的，紧接着 `process.exit()`
 * 会让 libuv 在句柄还没关完时退出，Windows 上直接断言崩溃
 * （`Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`，退出码 0xC0000409）。
 * 在云崽里这就是整个进程被带走，所以这里等一个 exit 事件。
 *
 * @returns {Promise<boolean>} 是否真的停掉了
 */
export async function stopService({ timeout = 5000 } = {}) {
  const proc = child
  if (!proc) return false
  child = null

  // 子进程和它的管道在 ensureRunning 里被 unref 过（不那样云崽会关不掉）。
  // 但 unref 的句柄**不会让事件循环保持存活**，循环一空进程就直接退出，
  // exit 事件永远等不到 —— 表现为 `unsettled top-level await` 警告 + 退出码 13。
  // 所以等之前先把句柄临时 ref 回来，等完再按需还回去。
  const handles = [proc, proc.stdout, proc.stderr].filter(h => typeof h?.ref === "function")
  for (const h of handles) {
    try {
      h.ref()
    } catch {
      /* 句柄已销毁 */
    }
  }

  const exited = await new Promise(resolve => {
    if (proc.exitCode !== null || proc.signalCode !== null) {
      resolve(true)
      return
    }
    const timer = setTimeout(() => resolve(false), timeout)
    proc.once("exit", () => {
      clearTimeout(timer)
      resolve(true)
    })
    try {
      proc.kill()
    } catch {
      clearTimeout(timer)
      resolve(true)
    }
  })

  // 没死透就把句柄还回 unref，免得反而把云崽吊住
  if (!exited) {
    for (const h of handles) {
      try {
        h.unref?.()
      } catch {
        /* 忽略 */
      }
    }
  }
  return exited
}
