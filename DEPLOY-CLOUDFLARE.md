# Cloudflare Pages 部署指引

`public/` 目录已完全符合 Cloudflare Pages 要求（单文件最大 2.71 MiB，限制 25 MiB）。
部署**不需要改任何代码**，只需在网页/账号侧配置一次。

---

## 前置：域名状态（已实测确认）

```
bearno1.dpdns.org
  NS    → hal.ns.cloudflare.com
          magali.ns.cloudflare.com
  SOA   → dns.cloudflare.com（serial 2414631501，TTL 1800）
  A     → （空）
  AAAA  → （空）
  CNAME → （空）
```

**结论**：该子域**已在你的 Cloudflare 账号中托管**（NS 已指向 Cloudflare），
所以可以直接在 Pages 里绑定自定义域，无需改 NS。

父域 `dpdns.org` 的 NS 是 `ns1~4.digitalplat.org`，但子域已单独委派给 Cloudflare，互不影响。

---

## 方案 A：GitHub 仓库直连 Pages（推荐，无需 API Token）

**为什么推荐**：不需要创建/传递任何密钥；GitHub Actions 每次提交产物后，
Cloudflare 自动重新部署，天然与自动更新链路同步。

### 步骤

1. 登录 https://dash.cloudflare.com
2. 左侧 **Workers & Pages** → **Create** → **Pages** → **Connect to Git**
3. 选择仓库 `bearnozhang/tv-hub` → **Authorize** GitHub
4. 构建设置：

   | 项 | 值 |
   |---|---|
   | Framework preset | `None` |
   | Build command | **留空**（不构建，产物已提交在仓库里） |
   | Build output directory | `public` |
   | Root directory | `/` （留空） |

5. **Deploy**
6. 部署成功后进入 **Settings → Custom domains** → **Set up a custom domain**
   → 填 `bearno1.dpdns.org` → 等待 DNS 自动验证

### 建议同时设置仓库变量（让看板显示真实订阅地址）

仓库 `tv-hub` → **Settings → Secrets and variables → Actions → New repository variable**

- Name: `TVHUB_BASE_URL`
- Value: `https://bearno1.dpdns.org`

设置后，下次 Actions 运行生成的 `index.html` 与 `status.json` 会显示你的域名，
而不是 raw 地址。**这一步可后补**，不影响部署。

---

## 方案 B：wrangler CLI（本机一条命令，需你提供 API Token）

我**不建议**把 API Token 给我。更安全的做法是你自己在本机执行：

```bash
# 1. 安装
npm i -g wrangler

# 2. 登录（浏览器授权，不需要你把 token 贴给我）
wrangler login

# 3. 创建 Pages 项目并部署
cd C:/Users/Administrator/WorkBuddy/2026-10-04-12-34-36/tv-hub
wrangler pages project create tv-hub --production-branch=main
wrangler pages deploy public --project-name=tv-hub --branch=main
```

绑定自定义域（需API Token 或网页操作）：

```bash
wrangler pages project list          # 确认项目存在
```

Token 权限需要：`Account → Cloudflare Pages → Edit`

### 本机当前状态（已实测）

```
wrangler           未安装
CF_* 环境变量       无
~/.wrangler        不存在
~/.config/.wrangler 不存在
```

---

## 方案 C：不用 Cloudflare（现在就可用）

GitHub raw 地址现在就是有效订阅地址：

```
https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/tv.json
https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/live.json
https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/status.json
```

实测 `tv.json` HTTP 200 / 2,837,001 B。

**唯一缺点**：国内直连 `raw.githubusercontent.com` 常常很慢或超时，
jsDelivr 加速更稳：

```
https://cdn.jsdelivr.net/gh/bearnozhang/tv-hub@main/public/tv.json
```

**Cloudflare 自定义域名的核心价值就是解决这个**：走 Cloudflare 全球 CDN，
国内访问通常比 raw 快得多。所以值得部署。

---

## 部署后的验收

```bash
# 1. 首页
curl -s -o /dev/null -w "%{http_code}\n" https://bearno1.dpdns.org/

# 2. 三个产物
for f in tv.json live.json status.json; do
  curl -s -o /dev/null -w "$f -> %{http_code}  %{size_download} B\n" \
    https://bearno1.dpdns.org/$f
done

# 3. 内容校验
curl -s https://bearno1.dpdns.org/tv.json | python -c "
import json,sys
d=json.load(sys.stdin)
print('sites=%d parses=%d lives=%d'%(len(d['sites']),len(d['parses']),len(d['lives'])))"
```

三个都应返回 HTTP 200，且 `tv.json` 能解析出 4334 个站点。

---

## 与自动更新的关系

Cloudflare Pages 与 GitHub Actions 是两条独立链路，互不依赖：

- **Actions** 负责抓取、校验、去重、合并，把产物提交回仓库 `public/`
- **Pages** 负责托管仓库里的 `public/`，把提交自动发布成网页

所以即使 Pages 部署失败，Actions 仍会照常更新 `public/`，
订阅地址退回到 raw / jsDelivr 即可使用，不影响功能。

---

## 回滚

删除 Pages 项目不会影响仓库与 Actions。若需撤销自定义域名，
在 Pages 设置里移除 `bearno1.dpdns.org` 即可，仓库侧无需任何改动。