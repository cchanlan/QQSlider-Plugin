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

/** 子进程句柄（只有我们自己拉起来的才记，别人起的不管） */
let child = null
let starting = null

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

      // 把 Node 路径透给 Python 侧：nvm / 非默认 PATH 的环境里裸 `node` 可能找不到
      const env = { ...pipEnv() }
      const nodeBin = findNode()
      if (nodeBin) env.QQ_SLIDER_NODE = nodeBin

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
