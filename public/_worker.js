/**
 * tv-hub Worker —— 强制不压缩响应，兼容老版本 TVBox / 影视仓客户端。
 *
 * 为什么需要：Cloudflare Pages/Workers 静态资产会按 Accept-Encoding
 * 返回 Brotli/gzip，但讴歌/影视仓的老 OkHttp 不支持解压，
 * 拿到二进制流 → JSON 解析失败 → 「你可能推送的是线路……」。
 *
 * 做法：静态资产通过 ASSETS 绑定取回，子请求带 Accept-Encoding: identity，
 * 保证边缘返回未压缩原文；响应侧再显式删除 Content-Encoding / Vary，
 * 并按扩展名补正确的 Content-Type。
 */

const NO_COMPRESS_TYPES = /\.(json|txt|m3u|html|xml)$/i;

const CT = [
  [/\.json$/i, "application/json; charset=utf-8"],
  [/\.txt$/i, "text/plain; charset=utf-8"],
  [/\.m3u$/i, "application/x-mpegurl; charset=utf-8"],
  [/\.html$/i, "text/html; charset=utf-8"],
  [/\.xml$/i, "application/xml; charset=utf-8"],
];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const upstream = new Request(url, request);
    upstream.headers.set("Accept-Encoding", "identity");

    const response = await env.ASSETS.fetch(upstream);

    if (!NO_COMPRESS_TYPES.test(url.pathname)) {
      return response;
    }

    const headers = new Headers(response.headers);
    headers.delete("Content-Encoding");
    headers.delete("Content-Length");
    headers.delete("Vary");

    // 特殊处理：.txt 里有两种内容 ——
    //   live.txt          是真纯文本直播源 → text/plain
    //   其余（sub/live-sub/live-tier）.txt 实际是 JSON → application/json
    const p = url.pathname;
    let ct = "text/plain; charset=utf-8";
    for (const [re, v] of CT) {
      if (re.test(p)) { ct = v; break; }
    }
    if (p.endsWith(".json")) ct = "application/json; charset=utf-8";
    if (p === "/live.txt") ct = "text/plain; charset=utf-8";
    if (/sub\.txt$|live-(mini|lite|standard|full)\.txt$/.test(p)) {
      ct = "application/json; charset=utf-8";
    }
    headers.set("Content-Type", ct);
    headers.set("Cache-Control", "public, max-age=300");
    headers.set("Access-Control-Allow-Origin", "*");

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  },
};
