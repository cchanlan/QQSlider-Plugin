/**
 * 配置加载 —— 三个框架（TRSS-Yunzai / Miao-Yunzai / JiuLi）都能用
 *
 * 首选框架自带的 `lib/plugins/config.js`：三家都有这个文件（JiuLi 那侧它只是
 * `core/plugin-config.js` 的兼容层），走它才能让配置文件位置、热重载、
 * 锅巴面板读写都跟着框架走。
 *
 * 用**动态 import + 兜底**而不是顶层静态 import，是因为静态 import 一旦失败
 * 整个插件就加载不了（插件列表里直接报错）。fork 改过结构、或插件被装到
 * 非标准位置时，兜底那条路仍能让插件起来。
 *
 * 兜底方案：自己读写 `config/<name>.yaml`（宿主有 yaml 模块就用），
 * 拿不到 yaml 就退成同名 `.json`。
 */

import fs from "node:fs"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_DIR = path.join(__dirname, "..")

/** 找框架根目录：从插件目录往上走，认第一个带 lib/plugins 的目录 */
export function findRoot() {
  let dir = PLUGIN_DIR
  for (let i = 0; i < 6; i++) {
    const parent = path.dirname(dir)
    if (parent === dir) break
    dir = parent
    if (fs.existsSync(path.join(dir, "lib", "plugins"))) return dir
  }
  return null
}

/** 尽量拿到宿主的 yaml 模块（拿不到返回 null） */
async function loadYaml() {
  for (const spec of ["yaml", "js-yaml"]) {
    try {
      const mod = await import(spec)
      const lib = mod?.default || mod
      // yaml: parse/stringify；js-yaml: load/dump
      if (typeof lib?.parse === "function" && typeof lib?.stringify === "function") {
        return { parse: lib.parse, stringify: lib.stringify }
      }
      if (typeof lib?.load === "function" && typeof lib?.dump === "function") {
        return { parse: lib.load, stringify: lib.dump }
      }
    } catch {
      /* 试下一个 */
    }
  }
  return null
}

/**
 * 兜底：自己读写配置，不依赖框架的 makeConfig。
 * 读不到 / 解析失败都退回默认值，绝不抛出去。
 */
async function selfManaged(name, defaults) {
  const root = findRoot()
  const dir = path.join(root || PLUGIN_DIR, "config")
  const YAML = await loadYaml()
  const file = path.join(dir, `${name}.${YAML ? "yaml" : "json"}`)

  const config = { ...defaults }
  try {
    if (fs.existsSync(file)) {
      const raw = fs.readFileSync(file, "utf8")
      const data = YAML ? YAML.parse(raw) : JSON.parse(raw)
      if (data && typeof data === "object") Object.assign(config, data)
    }
  } catch (err) {
    globalThis.logger?.warn?.(`[QQSlider] 配置读取失败，改用默认值：${err?.message || err}`)
  }

  const configSave = async () => {
    try {
      fs.mkdirSync(dir, { recursive: true })
      fs.writeFileSync(file, YAML ? YAML.stringify(config) : JSON.stringify(config, null, 2), "utf8")
    } catch (err) {
      globalThis.logger?.warn?.(`[QQSlider] 配置保存失败：${err?.message || err}`)
    }
  }

  await configSave()
  return { config, configSave, configFile: file }
}

/**
 * 建配置。
 *
 * @param {string} name 配置文件名（不带扩展名）
 * @param {object} defaults 默认值
 * @param {object} [keep] 强制保持的值（交给框架 makeConfig 的第三参）
 * @returns {Promise<{config: object, configSave: Function, configFile: string}>}
 */
export async function makePluginConfig(name, defaults, keep = {}) {
  const root = findRoot()
  if (root) {
    try {
      const mod = await import(pathToFileURL(path.join(root, "lib", "plugins", "config.js")).href)
      if (typeof mod?.default === "function") {
        return await mod.default(name, { ...defaults }, keep)
      }
    } catch (err) {
      globalThis.logger?.debug?.(`[QQSlider] 框架 makeConfig 不可用，改用自带配置：${err?.message || err}`)
    }
  }
  return selfManaged(name, defaults)
}
