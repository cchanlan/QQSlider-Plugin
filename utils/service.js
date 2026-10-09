/**
 * 过码服务的生命周期管理 + 运行环境自检
 *
 * 负责：找 Python/Node → 建 venv → 装依赖 → 拉起服务 → 健康检查 → 退出时收摊
 *
 * 设计原则：**插件加载时不该阻塞云崽启动**，所以：
 *   · 服务已在跑 → 直接用
 *   · 没在跑 → 后台自动拉起
 *   · 依赖没装 → 后台自动装，装完自动起
 * 过码是登录时才用到的功能，启动时慢一点没关系，但绝不能卡住云崽。
 *
 * ⚠️ 三个框架（TRSS-Yunzai / Miao-Yunzai / JiuLi）与 Windows / Linux 都要成立：
 *   · 不写死任何平台的绝对路径（venv 解释器位置、pip 缓存目录都按平台推）
 *   · 不假设 `python3` 或 `python` 存在，两个都试并用 `--version` 验真
 *   · 子进程一律 shell:false + 参数数组，路径带空格也不会被拆
 */

import { spawn, spawnSync } from "node:child_process"
import crypto from "node:crypto"
import fs from "node:fs"
import os from "node:os"
import fsp from "node:fs/promises"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SERVICE_DIR = path.join(__dirname, "..", "service")
const VENV_DIR = path.join(SERVICE_DIR, ".venv")
const SERVER_PY = path.join(SERVICE_DIR, "server.py")
const REQUIREMENTS = path.join(SERVICE_DIR, "requirements.txt")
const READY_FLAG = path.join(VENV_DIR, ".deps-ok")

const isWin = process.platform === "win32"
const isMac = process.platform === "darwin"

/** 起不来时给用户的可照做提示（只说做什么，不解释为什么） */
export const FIX_HINT = "发 #滑块过码安装 可重试"

function venvPython() {
  return isWin
    ? path.join(VENV_DIR, "Scripts", "python.exe")
    : path.join(VENV_DIR, "bin", "python")
}

let _pythonCache
/**
 * 找一个可用的 Python 3.9+。结果缓存，避免重复探测（`--version` 有进程开销）。
 *
 * 平台差异：Windows 通常只有 `python`（`python3` 往往是 Microsoft Store 的
 * 假别名，执行会报错而不是「找不到」），另有 `py` 启动器；Linux / macOS 反过来
 * 通常只有 `python3`。所以两边都试，并且**必须用 `--version` 验真** ——
 * 假别名会返回非 0 或不含 "Python 3" 的内容。
 *
 * @returns {string|null} 可执行命令名
 */
export function findPython() {
  if (_pythonCache !== undefined) return _pythonCache
  const candidates = isWin
    ? ["python", "py", "python3", "python3.13", "python3.12", "python3.11", "python3.10", "python3.9"]
    : ["python3", "python", "python3.13", "python3.12", "python3.11", "python3.10", "python3.9"]
  for (const cmd of candidates) {
    try {
      const r = spawnSync(cmd, ["--version"], { encoding: "utf8", timeout: 10000, windowsHide: true })
      if (r.status === 0 && /Python\s+3\.(\d+)/.test(r.stdout + r.stderr)) {
        _pythonCache = cmd
        return cmd
      }
    } catch {
      /* 试下一个 */
    }
  }
  _pythonCache = null
  return null
}

/** 取 Python 主次版本号（如 "3.11"），拿不到返回 null */
export function pythonVersion(cmd = findPython()) {
  if (!cmd) return null
  try {
    const r = spawnSync(cmd, ["--version"], { encoding: "utf8", timeout: 10000, windowsHide: true })
    const m = /Python\s+3\.(\d+)/.exec(r.stdout + r.stderr)
    return m ? `3.${m[1]}` : null
  } catch {
    return null
  }
}

let _nodeCache
/**
 * 找一个可用的 Node（Python 侧的 tdc worker 要用它跑腾讯 `tdc.js`）。
 * 云崽本身就依赖 Node，正常都能找到；找不到时服务仍能起，只是过码会在
 * 「起 worker」那一步失败，所以这里只提示不阻断。
 */
export function findNode() {
  if (_nodeCache !== undefined) return _nodeCache
  const forced = (process.env.QQ_SLIDER_NODE || "").trim()
  if (forced) {
    _nodeCache = forced
    return forced
  }
  try {
    const r = spawnSync("node", ["--version"], { encoding: "utf8", timeout: 10000, windowsHide: true })
    _nodeCache = r.status === 0 && /^v\d+/.test((r.stdout || "").trim()) ? "node" : null
  } catch {
    _nodeCache = null
  }
  return _nodeCache
}

/**
 * venv 的 site-packages 目录（可能有多套，全给出来）。
 * 纯文件系统操作，给 `depsReady` 用，必须秒回。
 */
function venvSitePackages() {
  const dirs = []
  if (isWin) {
    dirs.push(path.join(VENV_DIR, "Lib", "site-packages"))
    return dirs
  }
  try {
    for (const name of fs.readdirSync(path.join(VENV_DIR, "lib"))) {
      if (/^python\d/.test(name)) dirs.push(path.join(VENV_DIR, "lib", name, "site-packages"))
    }
  } catch {
    /* venv 还没建 */
  }
  return dirs
}

/**
 * venv 里到底有没有 pip —— **只看文件，不启进程**。
 *
 * 为什么不能只看「解释器在不在」：Debian / Ubuntu 上没装 `python3-venv` 时，
 * `python3 -m venv` 会**以退出码 0 建出一个没有 pip 的 venv**（ensurepip 被剥离，
 * 只在 stderr 留一句警告）。只看解释器就会把它当成「装好了」，
 * 之后每次 `-m pip install` 都报 `No module named pip`。
 */
function venvHasPip() {
  for (const dir of venvSitePackages()) {
    try {
      if (fs.existsSync(path.join(dir, "pip", "__init__.py"))) return true
    } catch {
      /* 下一个 */
    }
  }
  return false
}

/** 检查依赖是否已装好（解释器 + pip + 就绪标记，三重判断） */
export function depsReady() {
  return fs.existsSync(venvPython()) && fs.existsSync(READY_FLAG) && venvHasPip()
}

/** pip 的 HTTP 缓存目录（按平台推，不写死绝对路径） */
export function pipCacheDir() {
  if (process.env.PIP_CACHE_DIR) return process.env.PIP_CACHE_DIR
  if (isWin) {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local")
    return path.join(base, "pip", "Cache")
  }
  if (isMac) return path.join(os.homedir(), "Library", "Caches", "pip")
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "pip")
}

/** 跑一条命令，实时把输出转给 logger */
/**
 * 跑一条命令。
 *
 * 两种超时都要有：
 *   · timeout        总时长上限
 *   · silenceTimeout **连续多久没有任何输出**就认为卡死
 * pip 挂住时是完全静默的（打完 "Looking in indexes" 就再无输出），
 * 只靠总超时会让用户干等十几分钟；这个参数把它压到两分钟内可诊断。
 */
function run(cmd, args, { cwd = SERVICE_DIR, env, timeout = 600000, silenceTimeout = 0, onLine } = {}) {
  return new Promise(resolve => {
    let proc
    try {
      proc = spawn(cmd, args, { cwd, shell: false, windowsHide: true, env: env || process.env })
    } catch (err) {
      resolve({ code: -1, error: err, out: "" })
      return
    }

    let out = ""
    let settled = false
    let hardTimer = null
    let silenceTimer = null

    const finish = r => {
      if (settled) return
      settled = true
      clearTimeout(hardTimer)
      clearTimeout(silenceTimer)
      if (r.code === -1) {
        try {
          proc.kill()
        } catch {
          /* 已经退出 */
        }
      }
      resolve(r)
    }

    hardTimer = setTimeout(
      () => finish({ code: -1, error: new Error("timeout"), out, killed: "timeout" }),
      timeout,
    )

    const armSilence = () => {
      if (!silenceTimeout) return
      clearTimeout(silenceTimer)
      silenceTimer = setTimeout(
        () => finish({ code: -1, error: new Error("stalled"), out, killed: "silence" }),
        silenceTimeout,
      )
    }
    armSilence()

    const onData = buf => {
      const text = String(buf)
      out += text
      armSilence()
      if (onLine) for (const line of text.split(/\r?\n/)) if (line.trim()) onLine(line)
    }
    proc.stdout?.on("data", onData)
    proc.stderr?.on("data", onData)

    proc.on("error", err => finish({ code: -1, error: err, out }))
    proc.on("close", code => finish({ code, out }))
  })
}

/** pip 要带的环境变量（关掉版本自检、统一输出编码） */
function pipEnv() {
  return {
    ...process.env,
    // pip 的自检会去写 HTTP 缓存；缓存目录异常时那一步会挂住，干脆关掉
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    // 中文 Windows 上 pip 输出常是 GBK，不统一成 UTF-8 日志里会是乱码
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  }
}

/** 备选镜像：默认清华源（国内直连 PyPI 经常超时），失败依次回落 */
function pipIndexes() {
  const custom = (process.env.QQSLIDER_PIP_INDEX || "").trim()
  return [
    custom,
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
  ].filter((v, i, arr) => v && arr.indexOf(v) === i)
}

/** 把子进程输出尾巴截出来当错误详情 */
function tail(text, n = 400) {
  const s = String(text || "").trim()
  return s.length > n ? `…${s.slice(-n)}` : s
}

/**
 * 把所有镜像源的失败原因归并成一句**能照做**的话。
 *
 * 为什么要归并：用户实测的报错长这样 ——
 *
 *     安装依赖失败：https://pypi.org/simple/:/root/jiuli/plugins/QQSlider-Plugin/
 *     service/.venv/bin/python: No module named pip
 *
 * 它把**源地址、解释器路径、真实原因**用冒号拼成一串，看着像「这个源不通」，
 * 于是用户会去换源 —— 而真因是 venv 里没有 pip，换一百个源都一样。
 *
 * 所以这里：
 *   · 按**去掉源地址后**的原因去重（同一个真因只报一次）
 *   · 认出几种常见真因，直接翻译成「该做什么」
 *
 * （导出是为了能单测这个纯函数，插件内部不当公开 API 用。）
 */
export function summarizeFailures(failures, fallback = "") {
  const list = failures.filter(Boolean)
  if (!list.length) return fallback || "所有镜像源都不可用"

  // 去掉每条的 `源：` 前缀，看是不是同一个原因
  const reasons = list.map(s => String(s).replace(/^https?:\/\/\S+?[：:]\s*/, "").trim())
  const uniq = [...new Set(reasons)]

  const joined = uniq.join(" ").toLowerCase()
  if (/no module named pip/.test(joined)) {
    return "虚拟环境里没有 pip（执行 apt install python3-venv python3-pip 后重试）"
  }
  if (/no module named (venv|ensurepip)/.test(joined)) {
    return "缺少 venv 组件（Debian/Ubuntu 上执行 apt install python3-venv 后重试）"
  }
  if (/could not find a version|no matching distribution/.test(joined)) {
    return `找不到可安装的版本，可能是网络或 Python 版本不匹配：${tail(uniq[0], 200)}`
  }
  if (/permission denied|errno 13/.test(joined)) {
    return `权限不足（插件目录不可写？）：${tail(uniq[0], 200)}`
  }
  if (/ssl|certificate|tls/.test(joined)) {
    return `HTTPS 证书校验失败（检查系统时间 / 代理）：${tail(uniq[0], 200)}`
  }

  // 原因各不相同时，报第一个 + 说明还有几个源也失败（别只报最后一个）
  const first = tail(uniq[0], 220)
  return uniq.length > 1 ? `${first}（另外 ${uniq.length - 1} 个源同样失败）` : first
}

/**
 * 探测 pip 缓存目录**站在这个 venv 的 Python 视角**能不能用。
 *
 * 为什么必须用 Python 探、不能用 Node 探：
 * Windows 上目录权限是按进程令牌算的，同一个用户的不同进程可能结果不同 ——
 * 实测本机 Node 写 `%LOCALAPPDATA%\pip\Cache` 成功，而 venv 里的 Python
 * 被拒（沙箱化的 AppContainer 令牌差异）。用 Node 探会得出「可写」，
 * 然后 pip 继续挂死。
 *
 * 为什么必须带超时：不可写时 pip 不是报错而是**挂住** ——
 * `tempfile` 在 Windows 上遇到 PermissionError 会去查 `isdir`/`access`
 * 再重试，实测在这里空转几十秒到几分钟。所以「超时」等同于「不可用」。
 *
 * @returns {Promise<boolean>} 是否**确定**可用（任何异常/超时都算不可用）
 */
async function cacheUsable(pythonBin, logger) {
  if (process.env.QQSLIDER_NO_CACHE === "1") return false
  const dir = pipCacheDir()
  try {
    fs.mkdirSync(dir, { recursive: true })
  } catch {
    return false
  }

  // 用环境变量传路径：Windows 路径里的反斜杠放进 -c 字符串会被转义搞乱
  const code = [
    "import os, tempfile",
    "d = os.environ['QQSLIDER_CACHE_PROBE']",
    "fd, n = tempfile.mkstemp(dir=d)",
    "os.close(fd)",
    "os.unlink(n)",
    "print('ok')",
  ].join("\n")

  const r = await run(pythonBin, ["-c", code], {
    env: { ...pipEnv(), QQSLIDER_CACHE_PROBE: dir },
    timeout: 15000,
    silenceTimeout: 12000,
  })

  if (r.code === 0 && r.out.includes("ok")) return true

  logger?.info?.(
    r.killed
      ? `pip 缓存目录响应异常（${dir}），本次跳过快取缓存`
      : `pip 缓存目录不可写（${dir}），本次跳过快取缓存`,
  )
  return false
}

/**
 * 启进程真跑一次 `-m pip --version`。
 *
 * 比 `venvHasPip()` 看文件更准（pip 目录在、但坏了/版本不兼容也能查出来），
 * 所以只在安装流程里用 —— 它有进程开销，不能进 `depsReady()`。
 */
async function pipWorks(pythonBin, logger) {
  if (!pythonBin || !fs.existsSync(pythonBin)) return false
  const r = await run(pythonBin, ["-m", "pip", "--version"], {
    env: pipEnv(),
    timeout: 30000,
    silenceTimeout: 15000,
  })
  if (r.code === 0 && /\bpip\s+\d/.test(r.out)) return true
  logger?.debug?.(`${path.basename(pythonBin)} -m pip 不可用：${tail(r.out, 200)}`)
  return false
}

/**
 * 下载官方 get-pip.py 到插件目录。
 *
 * 落盘在 `service/` 而不是系统临时目录：Windows 的 `%TEMP%` 在沙箱化进程里
 * 可能不可写，而插件目录是确定的、且安装完会删掉。
 *
 * @returns {Promise<string|null>} 落盘路径
 */
async function downloadGetPip(logger) {
  const target = path.join(SERVICE_DIR, ".get-pip.py")
  const urls = [
    "https://bootstrap.pypa.io/get-pip.py",
    "https://mirrors.aliyun.com/pypi/get-pip.py",
    "https://pypi.tuna.tsinghua.edu.cn/get-pip.py",
  ]
  for (const url of urls) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 60000)
    try {
      const res = await fetch(url, { signal: controller.signal })
      if (!res.ok) continue
      const text = await res.text()
      // 认一下内容：别把错误页 / 半截响应当脚本执行
      if (text.length < 10000 || !/import\s+sys/.test(text)) continue
      await fsp.writeFile(target, text, "utf8")
      return target
    } catch (err) {
      logger?.debug?.(`get-pip.py 下载失败（${url}）：${err?.message || err}`)
    } finally {
      clearTimeout(timer)
    }
  }
  return null
}

/**
 * venv 里没有 pip 时就地补上。
 *
 * ## 为什么要专门做这一步
 *
 * 用户实测（2026-10-08，HLbot / JiuLi，Debian）：日志里是
 * `安装依赖失败：https://pypi.org/simple/:/root/jiuli/plugins/QQSlider-Plugin/service/.venv/bin/python:
 * No module named pip` —— 注意它把**镜像源和解释器拼在了一条消息**里，
 * 看着像「源的问题」，实际每个源都死在同一个地方：**venv 里根本没有 pip**。
 *
 * 根因是 Debian/Ubuntu 没装 `python3-venv` 时，`python3 -m venv` **退出码 0**
 * 但建出的 venv 不含 pip（ensurepip 被剥离）。原来的代码只看退出码，
 * 于是「建 venv 成功 → 装依赖失败 → 用户重试 → 再次失败」死循环。
 *
 * ## 自愈顺序（代价从低到高，任一步成功即返回）
 *
 *   ① `-m ensurepip`            Python 自带，多数情况一步到位
 *   ② `venv --upgrade-deps`     让 venv 模块自己重装 pip
 *   ③ 系统 pip `--python <venv>` pip 22.3+ 支持往别的解释器里装
 *   ④ `get-pip.py`              前三步都不行时取官方引导脚本
 *   ⑤ 重建 venv（带系统包）      最后兜底：能看见系统的 pip 就能用
 *
 * @returns {Promise<{ok:boolean, how?:string, error?:string}>}
 */
async function repairPip(python, logger) {
  const log = m => logger?.info?.(m)
  const dbg = m => logger?.debug?.(m)
  const py = venvPython()

  // ① ensurepip
  log("虚拟环境里没有 pip，用 ensurepip 补上…")
  let r = await run(py, ["-m", "ensurepip", "--upgrade", "--default-pip"], {
    env: pipEnv(),
    timeout: 180000,
    silenceTimeout: 90000,
    onLine: dbg,
  })
  if (await pipWorks(py, logger)) return { ok: true, how: "ensurepip" }
  dbg(`ensurepip 没成功：${tail(r.out, 200)}`)

  // ② venv --upgrade-deps（venv 模块会顺带把 pip 装上）
  log("改用 venv --upgrade-deps 补 pip…")
  r = await run(python, ["-m", "venv", "--upgrade-deps", VENV_DIR], {
    env: pipEnv(),
    timeout: 300000,
    silenceTimeout: 120000,
    onLine: dbg,
  })
  if (await pipWorks(py, logger)) return { ok: true, how: "venv --upgrade-deps" }
  dbg(`venv --upgrade-deps 没成功：${tail(r.out, 200)}`)

  // ③ 借系统 pip 往 venv 里装（pip 22.3+ 才有 --python）
  if (await pipWorks(python, logger)) {
    log("用系统 pip 往虚拟环境里装 pip…")
    r = await run(python, ["-m", "pip", "install", "--upgrade", "--python", py, "pip"], {
      env: pipEnv(),
      timeout: 300000,
      silenceTimeout: 120000,
      onLine: dbg,
    })
    if (await pipWorks(py, logger)) return { ok: true, how: "系统 pip --python" }
    dbg(`系统 pip --python 没成功：${tail(r.out, 200)}`)
  }

  // ④ get-pip.py
  const bootstrap = await downloadGetPip(logger)
  if (bootstrap) {
    log("用官方 get-pip.py 补 pip…")
    r = await run(py, [bootstrap, "--no-warn-script-location"], {
      env: pipEnv(),
      timeout: 600000,
      silenceTimeout: 150000,
      onLine: dbg,
    })
    try {
      fs.rmSync(bootstrap, { force: true })
    } catch {
      /* 删不掉也无妨，下次会覆盖 */
    }
    if (await pipWorks(py, logger)) return { ok: true, how: "get-pip.py" }
    dbg(`get-pip.py 没成功：${tail(r.out, 200)}`)
  }

  // ⑤ 重建 venv，让它能看见系统已装的包（系统有 pip 时 venv 就能用）
  log("重建虚拟环境（带系统包）…")
  try {
    fs.rmSync(VENV_DIR, { recursive: true, force: true })
  } catch {
    /* 删不掉就让 venv 自己报错 */
  }
  r = await run(python, ["-m", "venv", "--system-site-packages", VENV_DIR], {
    env: pipEnv(),
    timeout: 300000,
    silenceTimeout: 120000,
    onLine: dbg,
  })
  if (await pipWorks(venvPython(), logger)) return { ok: true, how: "venv --system-site-packages" }

  return {
    ok: false,
    error:
      "虚拟环境里装不上 pip。" +
      (isWin
        ? "重装 Python（安装时勾选 pip）后发 #滑块过码安装"
        : "执行 apt install python3-venv python3-pip（或对应发行版的包名）后发 #滑块过码安装"),
  }
}

/**
 * 建 venv 并装依赖。**幂等**：已装好就直接返回。
 *
 * 缺什么装什么：没 venv 就建、没依赖就装、镜像不通就换下一个源。
 *
 * @param {object} opts
 * @param {object} [opts.logger]
 * @param {boolean} [opts.force] 忽略就绪标记，强制重装
 * @returns {Promise<{ok:boolean, skipped?:boolean, error?:string}>}
 */
export async function installDeps({ logger, force = false } = {}) {
  if (!force && depsReady()) return { ok: true, skipped: true }

  const python = findPython()
  if (!python) {
    return {
      ok: false,
      error: isWin
        ? "没找到 Python 3.9+，装好后发 #滑块过码安装"
        : "没找到 Python 3.9+（Debian/Ubuntu 上装 python3 与 python3-venv 后发 #滑块过码安装）",
    }
  }
  const pyVer = pythonVersion(python)
  if (pyVer && Number(pyVer.split(".")[1]) < 9) {
    return { ok: false, error: `Python ${pyVer} 版本过低，需要 3.9 以上` }
  }

  const log = m => logger?.info?.(m)
  const dbg = m => logger?.debug?.(m)

  // ── 1. venv ──────────────────────────────────────────────────────────
  if (!fs.existsSync(venvPython())) {
    // 上次建到一半的残骸会让 python -m venv 直接失败，先清掉
    if (fs.existsSync(VENV_DIR)) {
      log("清理上次未建成的虚拟环境…")
      try {
        fs.rmSync(VENV_DIR, { recursive: true, force: true })
      } catch {
        /* 删不掉就让它自己报错 */
      }
    }
    log(`创建虚拟环境（${python}${pyVer ? " " + pyVer : ""}）…`)
    const r = await run(python, ["-m", "venv", VENV_DIR], { logger, timeout: 180000, onLine: dbg })
    if (r.code !== 0) {
      const detail = tail(r.out)
      // Linux 上最常见的坑：只有 python3、没有 python3-venv
      const hint = /ensurepip|No module named venv/i.test(detail)
        ? "缺少 venv 组件，Debian/Ubuntu 上执行 apt install python3-venv 后重试"
        : `${r.error?.message || detail}`
      return { ok: false, error: `创建虚拟环境失败：${hint}` }
    }
  }

  // ── 1b. venv 里必须有 pip ────────────────────────────────────────────
  //
  // ⚠️ 这一步不能省：Debian/Ubuntu 缺 python3-venv 时，`python3 -m venv`
  // **退出码 0 但建出的 venv 不含 pip**，随后每个镜像源都报同一句
  // `No module named pip`（用户实测踩过，见 repairPip 的注释）。
  // 老用户升级上来时 venv 已存在、同样可能没有 pip，所以这里无条件检查。
  if (!(await pipWorks(venvPython(), logger))) {
    const fixed = await repairPip(python, logger)
    if (!fixed.ok) return { ok: false, error: `${fixed.error}。${FIX_HINT}` }
    log(`虚拟环境 pip 已就绪（${fixed.how}）`)
  }

  // ── 2. 用 venv 自己的 Python 探缓存可用性 ────────────────────────────
  const extraArgs = []
  if (!(await cacheUsable(venvPython(), logger))) extraArgs.push("--no-cache-dir")

  // ── 3. 装依赖：多镜像依次重试 ─────────────────────────────────────────
  let lastError = ""
  let installed = false
  // 所有源都失败时，原因往往是**同一个**（venv 坏了、缺编译工具、没网）。
  // 只报最后一个源会让人以为是「那个源的问题」，所以把每个源的原因都记下来。
  const failures = []

  for (const index of pipIndexes()) {
    log(`安装依赖（源：${index}）…`)
    const r = await run(
      venvPython(),
      [
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "-i", index,
        "-r", REQUIREMENTS,
        ...extraArgs,
      ],
      {
        logger,
        env: pipEnv(),
        timeout: 900000,
        // pip 卡住时是完全静默的，90 秒没动静就换源重来
        silenceTimeout: 90000,
        onLine: dbg,
      },
    )
    if (r.code === 0) {
      installed = true
      break
    }
    if (r.killed === "silence") {
      lastError = `源 ${index} 无响应（已等 90 秒无输出）`
      failures.push(lastError)
      log(`${lastError}，换下一个源`)
      // 卡住的源多半也写不了缓存，后续都别用缓存了
      if (!extraArgs.includes("--no-cache-dir")) extraArgs.push("--no-cache-dir")
      continue
    }
    // venv 里的 pip 中途坏掉时，所有源都会报同一句 `No module named pip`。
    // 这种情况换源毫无意义，直接再修一次 pip 然后原地重试。
    if (/No module named pip/i.test(r.out)) {
      log("虚拟环境的 pip 不可用，重新修复后再试…")
      const fixed = await repairPip(python, logger)
      if (fixed.ok) {
        log(`pip 已修复（${fixed.how}），重试当前源`)
        const retry = await run(
          venvPython(),
          ["-m", "pip", "install", "--disable-pip-version-check", "-i", index,
           "-r", REQUIREMENTS, ...extraArgs],
          { logger, env: pipEnv(), timeout: 900000, silenceTimeout: 90000, onLine: dbg },
        )
        if (retry.code === 0) {
          installed = true
          break
        }
        lastError = `${index}：${tail(retry.out)}`
        failures.push(lastError)
      } else {
        return { ok: false, error: `${fixed.error}。${FIX_HINT}` }
      }
      continue
    }
    lastError = `${index}：${r.error?.message || tail(r.out)}`
    failures.push(lastError)
    log("该源安装失败，换下一个源")
  }

  if (!installed) {
    return { ok: false, error: `安装依赖失败：${summarizeFailures(failures, lastError)}。${FIX_HINT}` }
  }

  // ── 4. 真验一遍能不能 import，别只看 pip 退出码 ────────────────────────
  const check = await run(
    venvPython(),
    ["-c", "import cv2,numpy,curl_cffi,requests;print('ok')"],
    { timeout: 120000, onLine: dbg },
  )
  if (check.code !== 0 || !check.out.includes("ok")) {
    return { ok: false, error: `依赖校验失败：${tail(check.out, 300)}。${FIX_HINT}` }
  }

  // ── 5. 点选识别模型（可选）：装不上不影响滑块 ─────────────────────────
  await ensureClickModel({ logger })

  await fsp.writeFile(READY_FLAG, new Date().toISOString(), "utf8")
  log("Python 环境就绪")
  return { ok: true }
}

/**
 * 下载「点选识别」用的 CLIP 模型（约 182MB）。
 *
 * **失败不算错**：模型只影响点选题型，滑块完全不需要它。
 * 所以这里只提示，不阻断安装 —— 用户装不上也不该连滑块都用不了。
 */
export async function ensureClickModel({ logger, force = false } = {}) {
  const log = m => logger?.info?.(m)
  const py = venvPython()
  if (!fs.existsSync(py)) return false

  const code = [
    "import sys",
    "sys.path.insert(0, sys.argv[1])",
    "import click_recognizer as cr",
    "ok = cr.ensure_model()",
    "print('click-model-ready' if ok else 'click-model-failed')",
  ].join("\n")

  // 下载 182MB，给足时间；静默超时防止网络僵死
  const r = await run(py, ["-c", code, SERVICE_DIR], {
    logger,
    timeout: 900000,
    silenceTimeout: 120000,
    onLine: m => {
      if (/已下载|下载/.test(m)) log(m)
      else logger?.debug?.(m)
    },
  })

  const ok = r.out.includes("click-model-ready")
  if (!ok) {
    log("点选识别模型没装上（不影响滑块），登录遇到点选时可发 #滑块过码安装 重试")
  } else {
    log("点选识别模型就绪")
  }
  return ok
}

/**
 * 给 `#滑块过码状态` 用的版本比对结果。
 *
 * ⚠️ 判据必须跟 `ensureRunning()` **完全一致**，否则会出现
 * 「状态说已是最新、实际却在跑旧代码」这种最坑人的不一致。
 * 所以这里不自己判断，直接复用同一套逻辑（见 `isOurService` / `diskSupportsCodeHash`）。
 *
 * @returns {Promise<{disk:string, running:string, stale:boolean, known:boolean, ours:boolean}>}
 *   · disk    磁盘上这份代码的指纹
 *   · running 正在跑的服务报的指纹（旧版服务为空串）
 *   · stale   **跑的是旧代码**（该重启了）
 *   · known   服务有没有报指纹（旧版服务报不了）
 *   · ours    端口上的是不是过码服务
 */
export async function codeHashInfo(baseUrl) {
  const disk = diskCodeHash()
  const info = await health(baseUrl)
  const ours = isOurService(info)
  const running = String(info?.env?.codeHash || "")
  return {
    disk,
    running,
    ours,
    known: !!running,
    // 跟 ensureRunning 同一套判据：
    //   磁盘代码有指纹能力 + 服务报不出或不一致 = 旧代码
    // （旧版服务的 /health 连 env 都没有，running 自然是空串，同样命中）
    stale: ours && diskSupportsCodeHash() && !!disk && (!running || running !== disk),
  }
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

/**
 * 算「磁盘上这份代码」的指纹，跟服务端 `/health` 报的 `codeHash` 比对。
 *
 * ## 要解决的问题
 *
 * `git pull` / `#更新` 只把新文件写到磁盘上，**已经在跑的 Python 进程
 * 还在内存里执行旧代码**。而 `ensureRunning()` 原来第一句就是
 * 「服务活着就复用」，于是会一直用那个旧进程 ——
 * 表现是「插件明明更新了，过码行为还是老的」，用户只能自己猜要去重启。
 *
 * 更麻烦的是**孤儿进程**：插件用 `unref()` 把 Python 子进程从事件循环里
 * 摘掉（不摘的话云崽关不掉，见 `ensureRunning` 的注释），代价是
 * **云崽退出时不会带走它** —— 于是重启云崽后，旧 Python 进程还占着端口，
 * 新插件拉不起自己的服务，只能用那个旧的。
 *
 * 这个函数就是判据：**指纹不一致 = 跑的是旧代码，该把服务换掉**。
 *
 * ## 算法必须跟 Python 侧 `server.py:_code_hash()` 完全一致
 *
 * 都是：按文件名排序 → 每个文件「文件名 + \0 + 内容 + \0」喂进 sha256 →
 * 取前 16 位十六进制。只算 `.py` / `.cjs`，不算 `.venv` / `.models` /
 * `__pycache__`（那些不是代码）。
 *
 * 两边**任何一处改动都要同步改**，否则会出现「明明是最新代码却一直重启」
 * 或「该重启却不重启」。所以这里不做任何「优化」（比如缓存），
 * 逻辑越笨越不容易漂。
 *
 * @returns {string} 16 位十六进制指纹；读不到文件时返回空串
 */
function diskCodeHash() {
  try {
    const names = fs
      .readdirSync(SERVICE_DIR)
      .filter(n => (n.endsWith(".py") || n.endsWith(".cjs")) && !n.startsWith("."))
      .sort()
    const hash = crypto.createHash("sha256")
    for (const name of names) {
      hash.update(name, "utf8")
      hash.update("\0", "utf8")
      try {
        hash.update(fs.readFileSync(path.join(SERVICE_DIR, name)))
      } catch {
        hash.update("<unreadable>", "utf8")
      }
      hash.update("\0", "utf8")
    }
    return hash.digest("hex").slice(0, 16)
  } catch {
    return ""
  }
}

/**
 * 问正在跑的服务：你跑的是哪份代码？
 *
 * @returns {Promise<string>} 服务报的 codeHash；老版本服务没有这个字段时返回空串
 */
async function runningCodeHash(baseUrl, timeout = 3000) {
  const info = await health(baseUrl, timeout)
  return String(info?.env?.codeHash || "")
}

/**
 * 确认 `/health` 回话的是**我们自己的过码服务**，不是端口上别的什么程序。
 *
 * 为什么要校验：`health()` 只判断「有没有 JSON 回话」，而
 * `restartService()` 会**杀进程** —— 万一 8767 被别的程序占了
 * （用户改了端口、或另一个插件用了同一个端口），
 * 不校验就会把无辜的进程杀掉。
 *
 * ## 两种历史形态都要认（这是「换掉旧服务」的前提）
 *
 * 服务自己的 `/health` 结构变过：
 *
 *   ① 早期版本：**扁平**的，`{status, ok, fail, rounds, total_s, successRate, avgSeconds, avgRounds}`
 *   ② 当前版本：多一个 `env`，里面有 `tdcServer` / `deps` / `codeHash` / `pid`
 *
 * ⚠️ **恰恰是①最需要被认出来** —— 它是「旧服务」，而
 * 「把旧服务换掉」这个功能只发生在旧服务身上。只认②的话，
 * 遇到①会判成「不是我们的服务」直接跳过，等于这个功能对最该修的情况失效。
 *
 * 判据取各版本都稳定存在的字段：`status === "ok"` 且带
 * `successRate` + `avgRounds` + `rounds` —— 这个组合足够独特，
 * 不会跟碰巧占了同端口的别的程序混淆。
 */
function isOurService(alive) {
  if (!alive || typeof alive !== "object") return false

  // ② 有 env：看服务特有字段
  const env = alive.env
  if (env && typeof env === "object") {
    if ("tdcServer" in env || "deps" in env || "codeHash" in env) return true
  }

  // ① 扁平形态：状态统计的组合
  return (
    alive.status === "ok" &&
    "successRate" in alive &&
    "avgRounds" in alive &&
    "rounds" in alive
  )
}

/**
 * 按端口找占用它的进程 PID。
 *
 * ## 为什么需要它（2026-10-08 用户报修的延伸）
 *
 * 新版服务的 `/health` 里有 `pid`，直接拿来杀就行。但**旧版服务没有这个字段**
 * —— 而「把旧服务换掉」恰恰是最需要它的场景（鸡生蛋）。
 * 所以这里退一步：从操作系统层面问「谁占着这个端口」。
 *
 * 只用系统自带命令，不引入依赖：
 *   · Linux  `ss -ltnpH` → 失败退 `lsof -ti` → 再退 `fuser`
 *   · Windows `netstat -ano` 取 LISTENING 行的最后一列
 *
 * @returns {number} PID；找不到返回 0
 */
function findPidOnPort(port) {
  const p = String(port || "").trim()
  if (!/^\d+$/.test(p)) return 0

  const isWin = process.platform === "win32"

  if (isWin) {
    const r = spawnSync("netstat", ["-ano", "-p", "tcp"], {
      encoding: "utf8",
      timeout: 10000,
      windowsHide: true,
    })
    if (r.status !== 0 && !r.stdout) return 0
    // 形如：  TCP    127.0.0.1:8767    0.0.0.0:0    LISTENING    12345
    for (const line of String(r.stdout || "").split(/\r?\n/)) {
      if (!/LISTENING/i.test(line)) continue
      const cols = line.trim().split(/\s+/)
      if (cols.length < 4) continue
      const local = cols[1] || ""
      // 只比对端口，避免 `:18767` 被 `:8767` 误命中
      if (!local.endsWith(`:${p}`)) continue
      const pid = Number(cols[cols.length - 1])
      if (Number.isInteger(pid) && pid > 0) return pid
    }
    return 0
  }

  // Linux：优先 ss（iproute2，几乎都有），从 `users:(("python",pid=123,fd=4))` 抓 pid
  const attempts = [
    ["ss", ["-ltnpH", `sport = :${p}`]],
    ["lsof", ["-ti", `tcp:${p}`, "-s", "TCP:LISTEN"]],
    ["fuser", [`${p}/tcp`]],
  ]
  for (const [cmd, args] of attempts) {
    const r = spawnSync(cmd, args, { encoding: "utf8", timeout: 10000 })
    const out = String(r.stdout || "") + String(r.stderr || "")
    if (!out.trim()) continue
    const m = out.match(/pid=(\d+)/) // ss
    if (m) return Number(m[1])
    const first = out.trim().split(/\s+/).find(s => /^\d+$/.test(s)) // lsof / fuser
    if (first) return Number(first)
  }
  return 0
}

/**
 * 拿某个 PID 的命令行，用来确认「这个进程确实是我们的过码服务」。
 *
 * 找不到命令（权限不足 / 进程已退出）时返回空串 —— 调用方按「不确认」处理。
 */
function processCommandLine(pid) {
  if (!pid) return ""
  if (process.platform === "win32") {
    // wmic 在新版 Windows 已弃用，用 PowerShell 的 CIM（Win8+ 都有）
    const r = spawnSync(
      "powershell",
      ["-NoProfile", "-NonInteractive", "-Command",
       `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`],
      { encoding: "utf8", timeout: 10000, windowsHide: true },
    )
    return String(r.stdout || "").trim()
  }
  const r = spawnSync("ps", ["-p", String(pid), "-o", "args="], {
    encoding: "utf8",
    timeout: 10000,
  })
  return String(r.stdout || "").trim()
}

/**
 * 服务进程退出（好让插件用新代码重新拉起）。
 *
 * 老版本服务没有 `/shutdown`，会返回 404 —— 那种情况下返回 false，
 * 由调用方决定要不要退化成「按 PID 杀」。
 *
 * @returns {Promise<boolean>} 是否成功让它退出
 */
async function shutdownService(baseUrl, timeout = 5000) {
  const base = String(baseUrl || "").replace(/\/+$/, "")
  if (!base) return false
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await fetch(`${base}/shutdown`, { signal: controller.signal })
    // 404 = 旧版服务没这个端点，算失败
    return res.ok
  } catch {
    // 服务收到请求后立刻退出时，连接可能被重置 —— 这其实也是「成功」的迹象，
    // 但不能确定，所以交给调用方用「等它真的不响应了」来确认。
    return false
  } finally {
    clearTimeout(timer)
  }
}

/**
 * 等到服务真的不响应了（最多等 seconds 秒）。
 *
 * @returns {Promise<boolean>} 是否确认已停
 */
async function waitStopped(baseUrl, seconds = 10) {
  const deadline = Date.now() + seconds * 1000
  while (Date.now() < deadline) {
    if (!(await health(baseUrl, 1500))) return true
    await new Promise(r => setTimeout(r, 400))
  }
  return false
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

/** 子进程句柄（只有我们自己拉起来的才记，别人起的不管） */
let child = null
let starting = null

/**
 * 磁盘上的代码有没有「报版本指纹」的能力。
 *
 * 判据就是 `server.py` 里有没有 `_code_hash` 这个定义。
 *
 * ## 为什么需要它（鸡生蛋问题）
 *
 * 「服务该不该换掉」要比对两个指纹，而**旧版服务根本报不出指纹**。
 * 光看「拿不到指纹」不能下结论 —— 那可能是：
 *   (a) 磁盘上是新版代码、跑着的是旧进程  → **该重启**（正是用户的场景）
 *   (b) 磁盘上本来就是旧版代码            → 不该重启（旧插件也不会走到这儿）
 *
 * 用「磁盘代码有没有这个能力」就能分开：**磁盘有、服务报不出 → 必是 (a)**。
 * 这个判据比「看服务版本号」更可靠，因为不依赖任何约定好的字段值。
 */
function diskSupportsCodeHash() {
  try {
    const src = fs.readFileSync(SERVER_PY, "utf8")
    return src.includes("_code_hash")
  } catch {
    return false
  }
}

/**
 * 防死循环：同一个地址短时间内因版本问题重启过几次。
 *
 * 正常情况重启一次就好了（新进程必然报得出指纹）。但如果**新进程也报不出**
 * （比如磁盘上的代码被改坏、或者端口上其实是别的程序），
 * 没有这个闸门就会「重启 → 还是旧 → 再重启」无限转。
 */
const restartGuard = new Map()
const RESTART_GUARD_LIMIT = 3
const RESTART_GUARD_WINDOW = 5 * 60 * 1000

function guardAllowsRestart(baseUrl) {
  const key = String(baseUrl || "")
  const now = Date.now()
  const hits = (restartGuard.get(key) || []).filter(t => now - t < RESTART_GUARD_WINDOW)
  if (hits.length >= RESTART_GUARD_LIMIT) {
    restartGuard.set(key, hits)
    return false
  }
  hits.push(now)
  restartGuard.set(key, hits)
  return true
}

/**
 * 确保服务在跑，且**跑的是磁盘上这份代码**。
 *
 * ## 为什么要比对代码指纹（2026-10-08 用户报修）
 *
 * 原来第一句是「`health()` 通就复用」，于是：
 *
 *     用户 git pull / #更新 更新插件 → 磁盘上 .py 是新的
 *     但 Python 进程还在内存里跑旧代码
 *     → ensureRunning 看到「服务活着」就复用
 *     → 过码行为还是老的，用户以为「更新没生效」
 *
 * 所以现在多一步：拿磁盘指纹跟服务 `/health` 报的指纹比。
 * **不一致就把它换掉**（优雅退出 + 重新拉起），不用用户自己想办法重启。
 *
 * ## 孤儿进程是这件事的放大器
 *
 * 插件用 `unref()` 把 Python 子进程从事件循环里摘掉（不摘云崽关不掉），
 * 代价是**云崽退出时不会带走它**。于是重启云崽后旧 Python 还占着端口，
 * 新插件拉不起自己的服务，只能用那个旧的 —— 而且旧进程**没有父进程管**，
 * 不主动清理就会一直留着。这里正好一并解决。
 *
 * @returns {Promise<{ok:boolean, started?:boolean, restarted?:boolean, error?:string}>}
 */
export async function ensureRunning({ baseUrl, port, logger, rounds, timeoutMs } = {}) {
  const alive = await health(baseUrl)

  if (alive) {
    // 端口上必须**确实是我们自己的服务**才敢做版本判断和重启 ——
    // 万一被别的程序占了，重启就等于杀无辜进程。
    if (!isOurService(alive)) {
      logger?.warn?.(
        `${baseUrl} 上有程序在响应，但不是过码服务（端口被占用？），不做处理`,
      )
      return { ok: true, started: false }
    }

    const running = String(alive?.env?.codeHash || "")
    const disk = diskCodeHash()
    const diskKnowsHash = diskSupportsCodeHash()

    // 什么情况算「跑的是旧代码」：
    //   ① 两边都有指纹且不一致        → 代码改过了
    //   ② 磁盘代码有指纹能力、服务报不出 → 服务是旧版本（用户的场景）
    const stale = diskKnowsHash && disk && (!running || running !== disk)

    if (!stale) {
      if (diskKnowsHash && !running) {
        logger?.debug?.("服务没报 codeHash，但磁盘代码也没有该能力，不做版本比对")
      }
      return { ok: true, started: false }
    }

    if (!guardAllowsRestart(baseUrl)) {
      logger?.warn?.(
        `过码服务反复跑旧代码（已重启 ${RESTART_GUARD_LIMIT} 次），先不重启了。` +
        "发 #滑块过码重启 手动试一次，或重启云崽",
      )
      return { ok: true, started: false }
    }

    logger?.info?.(
      running
        ? `过码服务跑的是旧代码（${running} → ${disk}），正在重启以加载新版本…`
        : "过码服务是旧版本（报不出代码指纹），正在重启以加载新版本…",
    )
    const stopped = await restartService({ baseUrl, logger, port, timeoutMs })
    if (!stopped) {
      return {
        ok: false,
        error: "过码服务代码已更新，但旧进程停不掉（请发 #重启 或重启云崽）",
      }
    }
    // 停掉后往下走正常的「拉起来」流程
  }

  // 并发调用只拉一次
  if (starting) return starting

  starting = (async () => {
    try {
      const deps = await installDeps({ logger })
      if (!deps.ok) return { ok: false, error: deps.error }

      const args = [SERVER_PY, "--port", String(port)]
      if (rounds) args.push("--rounds", String(rounds))
      logger?.info?.(`启动过码服务（端口 ${port}）…`)

      // 把 Node 路径透给 Python 侧：nvm / 非默认 PATH 的环境里裸 `node` 可能找不到
      const env = { ...pipEnv() }
      const nodeBin = findNode()
      if (nodeBin) env.QQ_SLIDER_NODE = nodeBin
      // ★ 把**插件侧的过码超时**透给 Python，让它据此算自己的墙钟预算。
      //
      // 为什么不能让两边各写一个常量：点选重试预算调大之后，
      // 实测有一轮跑满 165.9s 才成功，而插件侧默认 180s —— 
      // 两边硬编码必然会漂移，一旦 Python 侧预算超过插件超时，
      // 就会在**明明还能试**的时候被上层掐断，用户看到「过码超时」。
      // 所以这里传实际超时值，Python 侧按它留出余量（见 click_solver.deadline_s）。
      if (timeoutMs > 0) env.QQ_SLIDER_TIMEOUT_MS = String(Math.round(timeoutMs))

      child = spawn(venvPython(), args, {
        cwd: SERVICE_DIR,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
        env,
      })
      const relay = b => {
        for (const line of String(b).split(/\r?\n/))
          if (line.trim()) logger?.info?.(`[solver] ${line}`)
      }
      child.stdout?.on("data", relay)
      child.stderr?.on("data", relay)
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

      const ok = await waitReady(baseUrl, 90, logger)
      return ok ? { ok: true, started: true } : { ok: false, error: `服务启动超时。${FIX_HINT}` }
    } catch (err) {
      return { ok: false, error: `${err?.message || String(err)}。${FIX_HINT}` }
    } finally {
      starting = null
    }
  })()
  return starting
}

/**
 * 把服务换掉：先请它自己优雅退出，不行再强杀，最后确认端口真的空出来。
 *
 * 四种情况都要处理：
 *   ① 服务是我们拉起来的 → 手上有 `child` 句柄，`stopService()` 能干净收摊
 *   ② 服务是**孤儿进程**（上次云崽留下的，`unref` 的代价）→ 走 `/shutdown`
 *   ③ 新版服务能报 `pid` → 直接按 PID 杀
 *   ④ **旧版服务既没 `/shutdown` 也没 `pid`**（鸡生蛋）→ 从端口反查 PID
 *
 * ④ 是最要紧的一条：**「把旧服务换掉」恰恰只发生在旧服务身上**，
 * 而旧服务正好什么信息都不给。所以这里退到操作系统层面问「谁占着这个端口」。
 *
 * @returns {Promise<boolean>} 是否确认已停
 */
export async function restartService({ baseUrl, logger, port, timeoutMs } = {}) {
  const before = await health(baseUrl, 2500)
  if (!before) return true // 本来就没跑

  // 不是我们的服务就别动 —— 杀了就是误伤别人的进程
  if (!isOurService(before)) {
    logger?.warn?.(`${baseUrl} 上的程序不是过码服务，不重启它`)
    return true
  }

  // ① 我们自己拉起来的：走标准收摊（它会等 stdio 关干净）
  if (child) {
    logger?.info?.("停掉本插件拉起的过码服务…")
    if (await stopService()) return true
  }

  // ② /shutdown（新版服务提供）
  logger?.info?.("请求过码服务退出（/shutdown）…")
  if (await shutdownService(baseUrl)) {
    if (await waitStopped(baseUrl, 10)) return true
  } else {
    logger?.debug?.("服务没有 /shutdown（旧版本），改用 PID 结束它")
  }

  // ③ 新版 /health 里有 pid；④ 旧版没有就从端口反查
  let pid = Number(before?.env?.pid || 0)
  let how = "服务上报"
  if (!(pid > 0)) {
    let portNum = port
    if (!portNum) {
      try {
        portNum = new URL(baseUrl).port
      } catch {
        portNum = ""
      }
    }
    pid = findPidOnPort(portNum || 8767)
    how = "端口反查"
  }

  if (pid > 0 && pid !== process.pid) {
    // 杀之前确认这个进程确实是过码服务（端口反查可能查到别的程序）
    const cmdline = processCommandLine(pid)
    const looksOurs = !cmdline || /server\.py|QQSlider/i.test(cmdline)
    if (!looksOurs) {
      logger?.warn?.(`PID ${pid} 占着端口但不是过码服务，不杀它：${cmdline.slice(0, 120)}`)
      return false
    }

    logger?.warn?.(`结束过码服务（PID ${pid}，来源：${how}）`)
    try {
      process.kill(pid, "SIGTERM")
    } catch (err) {
      logger?.debug?.(`kill ${pid} 失败：${err?.message || err}`)
    }
    if (await waitStopped(baseUrl, 8)) return true

    logger?.warn?.(`PID ${pid} 没响应 SIGTERM，强制结束`)
    try {
      process.kill(pid, "SIGKILL")
    } catch {
      /* 已经没了 */
    }
    if (await waitStopped(baseUrl, 5)) return true
  }

  logger?.warn?.("过码服务停不掉，端口可能被别的进程占着")
  return false
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
    // 等 `close` 而不是 `exit`：`exit` 只代表进程没了，stdio 管道可能还在关；
    // 紧接着 process.exit() 会让 libuv 在句柄关闭途中退出并断言崩溃。
    // `close` 的语义就是「进程已退出且所有 stdio 流已关闭」，正好是我们要的边界。
    proc.once("close", () => {
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

  // 手动把管道收干净再返回。
  // 即使 `close` 因为 unref 过而没等到，这一步也能把句柄销毁掉，
  // 避免调用方紧接着 process.exit() 时撞上
  // `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`。
  for (const s of [proc.stdout, proc.stderr]) {
    try {
      s?.destroy?.()
    } catch {
      /* 已经关了 */
    }
  }

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

/**
 * 运行环境总览，给 #滑块过码状态 用。
 * ⚠️ 故意不在这里探 pip 缓存可用性 —— 那次探测在最坏情况下要等十几秒，
 * 而状态指令应当秒回。安装时会探，并把结论写进日志。
 */
export function envReport() {
  const py = findPython()
  const pyVer = pythonVersion(py)
  const node = findNode()
  const missing = []
  if (!py) missing.push("Python 3.9+")
  if (!node) missing.push("Node.js")
  return {
    python: py ? `${py}${pyVer ? " (" + pyVer + ")" : ""}` : null,
    node: node || null,
    depsReady: depsReady(),
    pipCache: pipCacheDir(),
    missing,
  }
}
