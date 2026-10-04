# 客户端「解析失败」诊断与修复

## 症状

在影视仓 / 讴歌里填入配置地址或订阅地址后，App 提示：

```
你可能推送的是线路，已经返回到首页刷新
```

然后无法解析。三份内容完全不同的配置（247 站 / 4334 站 / 完整配置）**提示完全一样**。

---

## 结论：Cloudflare 的压缩导致，不是数据问题

### 排查过程（每一步都实测）

**① 服务端数据正常**

```
subscriptions.json  HTTP 200  552 B  JSON 可解析  Content-Type: application/json
tv.json             HTTP 200  2,837,001 B  JSON 可解析  顶层 15 个标准键
```

不同 User-Agent（okhttp / Dalvik / Chrome / curl）响应**完全一致**，排除 UA 拦截。

**② DNS 与网络正常**

```
tv.bearno1.dpdns.org → 104.21.70.37 / 172.67.219.110（Cloudflare）
无 AAAA 记录（无IPv6 隐患）
直连测试：HTTP 200，连接耗时 0.09s
```

**③ 根因：Cloudflare 按 `Accept-Encoding` 压缩响应**

这是关键证据。同一份文件，不同请求头的响应：

| App 发出的请求头 | Cloudflare 响应 | 客户端能否解析 |
|---|---|---|
| 不发 `Accept-Encoding` | 无压缩 91,209 B | ✅ |
| `identity` | 无压缩 91,209 B | ✅ |
| **`gzip`** | **压缩为 9,765 B** | ❌ 拿到 gzip 二进制流 |
| **`gzip, deflate`** | **压缩为 9,765 B** | ❌ 同上 |
| **`br, gzip`**（现代默认） | **压缩为 10,898 B** | ❌ 拿到 Brotli 流 |

且响应头里 `Vary: None` —— Cloudflare **没有**按 `Accept-Encoding` 做协商，
任何客户端都会被压缩。

**现代 App 默认发 `Accept-Encoding: br, gzip`**，
而很多 TVBox 系 App 的 HTTP 客户端（老版本 OkHttp / 部分魔改内核）**不支持相应解压算法**，
拿到的是 `1f 8b 08` 开头的二进制流，JSON 解析必然失败 → 弹出那句提示。

### 为什么三份配置提示一样

因为它们走同一条 Cloudflare 链路、同样被压缩。**数据内容不同，但失败原因相同** ——
这正是排除「数据问题」、定位到「传输层问题」的关键线索。

---

## 立刻可用的方案（不用改任何东西）

**GitHub raw 不经Cloudflare，实测无压缩：**

```
https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/tv.json
```

实测对比：

| 来源 | Content-Encoding | 直接可解析 |
|---|---|---|
| `raw.githubusercontent.com` | **None** | **✅** |
| `cdn.jsdelivr.net` | gzip | ❌ |
| `tv.bearno1.dpdns.org` | gzip | ❌ |

**先用这个地址验证 App 本身没问题。** 能正常解析 → 确认是 Cloudflare 压缩所致。

---

## 根本修复：禁用 Cloudflare 压缩

### 方式一：Dashboard 设置（最简单，2 分钟）

Cloudflare 控制台 → `Workers 和 Pages` → `tv-hub` → `设置` → `转换`（Transformations）
→ 关闭 **"Brotli"** 与 **"Gzip"** 两项。

关闭后立即生效，无需重新部署。

### 方式二：Worker 强制 identity（更彻底）

仓库里的 `worker-no-compress.js` 会：

1. 转发时把上游请求的 `Accept-Encoding` 改成 `identity`，让边缘不返回压缩流
2. 响应头里删掉 `Content-Encoding` 与 `Vary`
3. 补上 `Content-Type: application/json; charset=utf-8`
4. 只对 `.json` / `.txt` / `.m3u` / `.html` / `.xml` 生效，其他资源照常

**注意**：你当前的 Worker 是「静态资源 + Git 集成」模式。要加这段代码，
需要把部署方式从静态资源切换为 Worker 脚本，或在现有 Worker 前再套一层。

#### 部署命令（本机执行，不需给我token）

```bash
cd C:/Users/Administrator/WorkBuddy/2026-10-04-12-34-36/tv-hub

# 1. 安装（已有可跳过）
npm i -g wrangler

# 2. 登录（浏览器授权，不要把 token 贴给我）
wrangler login

# 3. 上传 Worker（先在测试环境验证，确认无误再切生产）
wrangler deploy worker-no-compress.js --name tv-hub-compat
```

部署后用下面的命令验证是否已解除压缩：

```bash
curl -sI -H "Accept-Encoding: gzip" https://<你的域名>/tv.json | grep -i "content-encoding"
# 期望输出：Content-Encoding: 不出现（即已无压缩）
```

### 方式三：换回 GitHub raw（0 成本）

如果你不想动 Cloudflare 配置，直接用 raw 地址就行。
缺点是国内访问 raw 较慢，但**至少能用**。

---

## 修复后的验证清单

```bash
# 必须看到 Content-Encoding 消失
curl -sI -H "Accept-Encoding: gzip" https://tv.bearno1.dpdns.org/tv.json | grep -i content-encoding

# 确认内容可解析
curl -s -H "Accept-Encoding: gzip" https://tv.bearno1.dpdns.org/tv.json --output t.json
python -c "import json;print('sites=',len(json.load(open('t.json',encoding='utf-8'))['sites']))"
```

然后在 App 里清空旧配置 → 粘贴新地址 → 保存 → 完全退出App → 重新打开。

**清缓存很重要**：App 会缓存失败的配置，不清会一直报同样的错。

---

## 附：另外两个潜在隐患（本次不是元凶，但值得知道）

**① `wallpaper` 用的是中文域名**

```
"wallpaper": "https://深色壁纸.xxooo.cf/"
```

实测在标准 URL 库层面就抛 `UnicodeEncodeError`。
部分 TVBox 衍生版不做IDN punycode 转换，拉背景失败可能中断加载。
如果修复压缩后仍有问题，可以把这字段去掉（客户端会退化成默认背景）。

**② `spider` 伪装成 `.png`**

```
"spider": "https://img2.gelonghui.com/library/46da6-....png"
```

实际是标准 JAR（文件头 `504b0304` 已验证），这是上游的常见做法。
个别客户端会按扩展名拦截，若修复压缩后仍失败，再考虑这个问题。

排查用的对照配置已放在 `public/minimal/`：
- `minimal/api-only.json` —— 247 站，无 jar 无 wallpaper
- `minimal/tv.json` —— 4334 站，有 jar，无 wallpaper

排查完可以删掉。