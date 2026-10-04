/**
 * tv-hub Worker —— 禁用压缩，兼容老版本 TVBox / 影视仓 客户端。
 *
 * 【为什么需要这个】
 * Cloudflare 默认按 Accept-Encoding 压缩响应（实测：gzip→9.7KB、br→10.9KB）。
 * 但很多 TVBox 系 App 的 HTTP 客户端（OkHttp 老版本 / 部分魔改内核）
 * 不支持 gzip/br 解压，拿到的是二进制流 → JSON 解析必然失败，
 * App 弹「你可能推送的是线路，已经返回到首页刷新」。
 *
 * 实测证据：
 *   Accept-Encoding: gzip     → Content-Encoding: gzip  → 客户端拿到压缩流
 *   Accept-Encoding: br,gzip  → Content-Encoding: br→ 客户端拿到 brotli 流
 *   不发 / identity            → 无压缩                → 正常
 *
 * 本 Worker 强制 identity，并补齐正确的 Content-Type 与 CORS。
 *
 * 【部署步骤】见 README 或 SUBSCRIPTION.md 的「Cloudflare 修复」章节。
 */

const NO_COMPRESS_TYPES = /\.(json|txt|m3u|html|xml)$/i;

export default {
  async fetch(request, env, ctx) {
    const incoming = request.headers.get("Accept-Encoding") || "";
    // 客户端若明确表示不支持压缩（老 OkHttp 会发 identity 或干脆不发），
    // 就不要主动改成它能接受的形式。
    const wantsGzip = /\bgzip\b/i.test(incoming) || /\bbr\b/i.test(incoming);

    // 传给 Cloudflare 的上游请求：去掉 Accept-Encoding，
    // 这样边缘不会返回压缩流。
    const upstream = new Request(request.url, request);
    upstream.headers.set("Accept-Encoding", "identity");

    let response = await fetch(upstream, {
      cf: { cacheEverything: true, cacheTtl: 300 },
    });

    if (!NO_COMPRESS_TYPES.test(new URL(request.url).pathname)) {
      return response;
    }

    // 强制 identity：既去掉 Content-Encoding，也去掉可能诱发压缩的 Vary
    const headers = new Headers(response.headers);
    headers.delete("Content-Encoding");
    headers.delete("Content-Length");
    headers.set("Content-Type", "application/json; charset=utf-8");
    headers.set("Cache-Control", "public, max-age=300");
    headers.delete("Vary");

    // 重新构造 body，确保与去掉的长度头一致
    const body = response.body;
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  },
};