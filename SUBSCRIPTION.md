# 订阅地址与部署状态

## ✅ 已上线（2026-10-04 实测）

**自定义域（推荐使用，走 Cloudflare CDN）**

```
https://tv.bearno1.dpdns.org/tv.json          ← TVBox / 影视仓 配置地址
https://tv.bearno1.dpdns.org/live.json        ← 直播配置
https://tv.bearno1.dpdns.org/status.json      ← 状态
https://tv.bearno1.dpdns.org/                 ← 状态看板
```

**Worker 默认域（备用，同样实时）**

```
https://tv-hub.bearno1981.workers.dev/tv.json
```

**GitHub raw（兜底，国内可能慢）**

```
https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/tv.json
```

---

## 在TVBox / 影视仓 里怎么填

| 用途 | 填这个 |
|---|---|
| 主配置（影视仓/TVBox「配置地址」） | `https://tv.bearno1.dpdns.org/tv.json` |
| 直播源（设置 → 直播 → 添加直播源） | `https://tv.bearno1.dpdns.org/live.json` |

---

## 部署形态说明

实际部署的是 **Cloudflare Workers**（不是 Pages）：

- Worker 名：`tv-hub`
- 默认域：`tv-hub.bearno1981.workers.dev`
- 自定义域：`tv.bearno1.dpdns.org`
- 绑定方式：Git 集成（仓库 `bearnozhang/tv-hub`，分支 `main`）

### 已实测验证自动同步

方法：往 `public/subscriptions.json` 注入探针项并 push，观察线上。

结果：push 后**约 1 分钟内**自定义域返回了新内容（`urls` 从 4 变 5且含探针），
随后探针已revert（commit `ad0a0dd`），线上恢复为 4 个 URL。

**结论**：GitHub Actions 提交产物 → Cloudflare 自动重新发布，无需手工介入。

---

## 线上实测数据（2026-10-04 13:58）

| 文件 | HTTP | 大小 | 响应耗时 |
|---|---|---|---|
| tv.json | 200 | 2,837,001 B | 2.18s |
| live.json | 200 | 1,123,380 B | 0.98s |
| status.json | 200 | 8,848 B | 0.35s |
| subscriptions.json | 200 | 749 B | 0.32s |

`tv.json` 解析：sites 4334 / parses 222 / lives 609 / flags 60，key 唯一、type 全 int。
`status.json`：8 源全 ok，sites 8837 → 4334（去重 4503），直播 37 组 / 3765 频道。

---

## 三条链路的关系

```
上游仓库 (raw)  →  GitHub Actions 抓取/校验/去重/合并  →  提交 public/ 到 main
                                                            ↓
                                        Cloudflare Workers 自动发布（~1 分钟）
                                                            ↓
                                          tv.bearno1.dpdns.org 对外提供订阅
```

三条链路互不依赖：任一环节故障，其余仍可用（可退回 raw 地址）。

---

## 可选优化

### 让看板显示真实域名

仓库 `tv-hub` → **Settings → Secrets and variables → Actions → New repository variable**

- Name: `TVHUB_BASE_URL`
- Value: `https://tv.bearno1.dpdns.org`

下次 Actions 运行后，`index.html` 与 `status.json` 的 `base_url` 字段会变成你的域名，
看板底部的订阅地址也会显示 `tv.bearno1.dpdns.org` 而非 raw 地址。**可后补，不影响使用。**

### 若想用根域名 `bearno1.dpdns.org`（不带 `tv.` 前缀）

当前绑的是 `tv.bearno1.dpdns.org`。要换成根域名，需在
**Workers 和 Pages → tv-hub → 设置 → 域和路由 → 添加自定义域** 里
再加 `bearno1.dpdns.org`，然后把 `TVHUB_BASE_URL` 改成根域名。

注意：根域若已被其他服务占用（如平板/路由面板），需先确认无冲突。

---

## 常见问题

**Q：Worker 域返回 5208 字节，自定义域返回 6146 字节，内容不一样？**

A：正常。差异是 Cloudflare 向自定义域注入的 JS 挑战脚本（938 B），
产品内容完全一致。用 `diff` 对比过，除该脚本外无任何差异。

**Q：数据多久更新一次？**

A：上游每天两次更新（北京时间 10:05 / 22:05 定时任务），
Actions 跑完后 Cloudflare 约 1 分钟内跟上。
若某个上游抓取失败，会沿用上一次成功的结果并在 `status.json` 标记 `stale`。

**Q：为什么不用 raw 地址？**

A：raw 是可用的，但国内直连 `raw.githubusercontent.com` 经常很慢甚至超时。
Cloudflare CDN 国内访问通常快得多。