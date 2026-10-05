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

const NO_COMPRESS_TYPES = /\.(json|txt|m3u|html|xml|jar)$/i;

// 无扩展名别名：某些 TVBox 改版只认 `http://host/tv` 这种形态
// （实测饭太硬就是 `http://fty.xxooo.cf/tv`，返回 JSON 但路径没有 .json）。
// 这里把无扩展名路径映射到实际资产，并按目标类型返回正确 Content-Type。
const ALIAS = {
  "/tv": "/tv.json",
  "/tv.json": "/tv.json",
  "/api": "/tv.json",
  "/config": "/tv.json",
  "/live": "/live.txt",
  "/livetxt": "/live.txt",
  "/tvbox": "/tv.json",
  "/tvs": "/tvs.json",
  "/lite": "/tv-lite.json",
  // 高速精选：只含「有速度证据且够快」的源（见 scripts/curate.py）
  "/fast": "/tv-fast.json",
  "/tv-fast": "/tv-fast.json",
  "/quick": "/tv-fast.json",
  // 相对路径源专用档：仅 FongMi 系（讴歌/影视仓）客户端可用。
  // 主流 TVBox 官方版不认 `./` 开头的 api，会整体「解析配置失败」，
  // 所以这些源从主档剔出、单独成档。见 merge.write_deps_tier。
  "/deps-config": "/tv-deps.json",
  "/tvdeps": "/tv-deps.json",
  // 极简诊断档：12 个标准采集站，不带 spider/lives/parses/flags。
  // 用于二分定位「配置解析失败」到底出在哪一层。
  "/safe": "/tv-safe.json",
  "/test": "/tv-safe.json",
  // 对照诊断档：每档只比 /safe 多一个变量，逐档试可锁定出错字段
  "/t1": "/tv-t1.json",   // + 我们的 spider
  "/t2": "/tv-t2.json",   // + 参照 spider（ysc 用的，已验证可用）
  "/t3": "/tv-t3.json",   // + lives
  "/t4": "/tv-t4.json",   // + parses
  "/t5": "/tv-t5.json",   // + flags
  // 极小档（低带宽环境验证：最小 jar 175B vs 原 915KB）
  "/m1": "/tv-m1.json",   // 3 站，无 spider
  "/m2": "/tv-m2.json",   // 3 站 + 最小 jar
  // 「实测可达」档：只用本项目 CI 实际探测通过的源（40 个，按速度排序）
  "/ok1": "/tv-ok1.json",  // 不带 jar
  "/ok2": "/tv-ok2.json",  // 带真实 jar
};

const CT = [
  [/\.json$/i, "application/json; charset=utf-8"],
  [/\.txt$/i, "text/plain; charset=utf-8"],
  [/\.m3u$/i, "application/x-mpegurl; charset=utf-8"],
  [/\.html$/i, "text/html; charset=utf-8"],
  [/\.xml$/i, "application/xml; charset=utf-8"],
];

// ── /deps/* 同源代理 ──────────────────────────────────────────────────
//
// 为什么需要：
//   上游 hebijunge 整套配置建立在 `./deps/...` **相对路径**上 —— 它的
//   spider 就是 `./deps/feishu-sync/一木源/JAR/XB包jar/LIBVIO.jar;md5;...`，
//   站点 ext 也大量写作 `./deps/auto/.../xxx.js`。客户端以「配置所在目录」
//   为基准拼接，因此在同源部署下这些路径是可用的。
//
//   我们保留这些相对路径以救活那批源（此前一刀切删除，导致 307 个源变空壳），
//   但仓库里并没有 deps 目录（上游 deps 有 1.1GB / 1.2 万文件，全量镜像不现实）。
//   因此由 Worker 反代到上游镜像仓库 —— **零存储、零 CI 成本**。
//
// 安全：只放行 /deps/ 前缀，拒绝 `..`，防目录逃逸/SSRF。
const DEPS_PREFIX = "/deps/";
const DEPS_UPSTREAMS = [
  "https://raw.githubusercontent.com/hebijunge/tvbox-config/main",
  "https://cdn.jsdelivr.net/gh/hebijunge/tvbox-config@main",
];

function depsContentType(p) {
  if (/\.jar$/i.test(p)) return "application/java-archive";
  if (/\.js$/i.test(p)) return "application/javascript; charset=utf-8";
  if (/\.json$/i.test(p)) return "application/json; charset=utf-8";
  if (/\.txt$/i.test(p)) return "text/plain; charset=utf-8";
  if (/\.html?$/i.test(p)) return "text/html; charset=utf-8";
  return "application/octet-stream";
}

async function proxyDeps(url) {
  const rel = url.pathname.slice(1); // "deps/xxx/yyy.js"
  if (rel.includes("..") || rel.includes("\\")) {
    return new Response("bad path", { status: 400 });
  }
  for (const base of DEPS_UPSTREAMS) {
    try {
      const r = await fetch(`${base}/${rel}`, {
        headers: { "User-Agent": "tv-hub-worker/1.0", "Accept-Encoding": "identity" },
        cf: { cacheTtl: 86400, cacheEverything: true },
      });
      if (!r.ok) continue;
      const h = new Headers(r.headers);
      h.delete("Content-Encoding");
      h.delete("Content-Length");
      h.delete("Vary");
      h.set("Content-Type", depsContentType(rel));
      h.set("Cache-Control", "public, max-age=86400");
      h.set("Access-Control-Allow-Origin", "*");
      return new Response(r.body, { status: 200, headers: h });
    } catch (e) {
      // 换下一个上游
    }
  }
  return new Response("deps not found", { status: 404 });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // 上游依赖（deps）同源反代
    if (url.pathname.startsWith(DEPS_PREFIX)) {
      return proxyDeps(url);
    }

    // 无扩展名别名 → 实际资产
    const alias = ALIAS[url.pathname];
    if (alias) {
      const assetUrl = new URL(url.origin + alias);
      assetUrl.search = url.search;
      const ua = new Request(assetUrl, request);
      ua.headers.set("Accept-Encoding", "identity");
      const ar = await env.ASSETS.fetch(ua);
      const ah = new Headers(ar.headers);
      ah.delete("Content-Encoding");
      ah.delete("Content-Length");
      ah.delete("Vary");
      ah.set(
        "Content-Type",
        alias.endsWith(".txt")
          ? "text/plain; charset=utf-8"
          : "application/json; charset=utf-8",
      );
      ah.set("Cache-Control", "public, max-age=300");
      ah.set("Access-Control-Allow-Origin", "*");
      return new Response(ar.body, {
        status: ar.status,
        statusText: ar.statusText,
        headers: ah,
      });
    }

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
    // jar 必须以 java-archive 返回：部分 TVBox 内核按 Content-Type 判断
    // 是否是可加载的 spider 包，image/png 会被直接拒绝 → 报「解析配置失败」
    if (p.endsWith(".jar")) ct = "application/java-archive";
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
