/**
 * 过码服务客户端
 *
 * 只负责「把一个滑块 URL 变成 ticket」，服务的起停交给 utils/service.js。
 */

import { ensureRunning, health } from "./service.js"

/**
 * 让服务把一个滑块 URL 解成 ticket。
 *
 * @param {object} opts
 * @param {string} opts.url       滑块验证 URL
 * @param {string|number} [opts.uin] QQ 号
 * @param {string} opts.baseUrl   服务地址
 * @param {number} [opts.port]    服务端口（没在跑时要拉起来）
 * @param {number} [opts.timeout] 超时毫秒
 * @param {object} [opts.logger]
 * @returns {Promise<string>} ticket
 */
export async function solveSlider({
  url,
  uin = "",
  baseUrl,
  port,
  timeout = 120000,
  logger,
} = {}) {
  if (!url) throw new Error("缺少滑块 URL")
  const base = String(baseUrl || "").replace(/\/+$/, "")
  if (!base) throw new Error("过码服务地址为空")

  // 没在跑就先拉起来（首次会装依赖，可能久一点）
  const ready = await ensureRunning({ baseUrl: base, port, logger })
  if (!ready.ok) throw new Error(ready.error || "过码服务启动失败")

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), Number(timeout) || 120000)
  let res
  try {
    res = await fetch(`${base}/solve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, uin: uin ? String(uin) : "" }),
      signal: controller.signal,
    })
  } catch (err) {
    if (err?.name === "AbortError") throw new Error(`过码超时（${timeout}ms）`)
    throw new Error(`过码服务不可用：${err?.message || err}`)
  } finally {
    clearTimeout(timer)
  }

  if (!res.ok) throw new Error(`过码服务返回 HTTP ${res.status}`)
  const data = await res.json().catch(() => null)
  if (!data) throw new Error("过码服务返回的不是 JSON")

  logger?.debug?.(`过码结果 ${JSON.stringify(data)}`)

  if (!data.ok || !data.ticket) {
    const why = data.error || `errorCode=${data.errorCode}`
    if (data.kind && data.kind !== "slide") {
      throw new Error(`服务端下发的是「${data.kind}」题型，本方案只解滑块：${why}`)
    }
    throw new Error(`过码失败：${why}`)
  }
  return data.ticket
}

export { health }
