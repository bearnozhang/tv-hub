# 订阅地址

## ✅ 你要填的地址（只填这一个）

```
https://tv.bearno1.dpdns.org/subscriptions.json
```

**影视仓 / 讴歌 都填这个。** App 读取后会自动列出下面 4 个仓，你在 App 内点选切换，
不需要再逐个手动填别的地址。

### App 内会看到的清单（2026-10-04 线上实测）

| # | 名称 | 站点数 | 实际内容 |
|---|---|---|---|
| 1 | ★ tv-hub 全量聚合（推荐） | **4334** | 全部上游合并去重 + 直播 + 222 解析器 |
| 2 | hebijunge·TVBox 全量 | 4286 | 单一上游原味（已清洗） |
| 3 | 影视仓·单仓聚合 | 167 | 轻量版，加载快 |
| 4 | hebijunge·纯点播 | 4278 | 只有影视、没有直播 |

线上逐条实测：4 个条目全部 HTTP 200 且解析正常。

---

## 位置

| App | 位置 |
|---|---|
| 影视仓 | 设置 → 配置地址 → 粘贴 → 确定 |
| 讴歌 | 设置 → 配置 / 订阅 → 粘贴 → 确定 |

---

## 如果你的 App 只认「单个配置」

有些版本不支持多仓切换，只支持填一个直接的配置。那就填第一个（推荐项）：

```
https://tv.bearno1.dpdns.org/tv.json
```

这个就是全量聚合，4334 个站点 + 直播，一次到位。

---

## 直播要单独填

```
https://tv.bearno1.dpdns.org/live.json
```

**位置**：设置 → 直播 → 添加直播源

37 个分组 / 3765 个频道，761 个频道带多备源（播放失败自动换源）。

---

## 备用地址（自定义域不通时）

```
https://tv-hub.bearno1981.workers.dev/subscriptions.json
https://tv-hub.bearno1981.workers.dev/tv.json

https://raw.githubusercontent.com/bearnozhang/tv-hub/main/public/subscriptions.json
https://cdn.jsdelivr.net/gh/bearnozhang/tv-hub@main/public/tv.json
```

---

## 全部产物

| 路径 | 用途 |
|---|---|
| `/subscriptions.json` | **订阅清单**（填这个） |
| `/subscriptions.detail.json` | 同上 + note 说明，给人看 |
| `/tv.json` | 全量聚合配置 |
| `/live.json` | 直播配置 |
| `/profiles/hebi_tvbox.json` | 单仓：hebijunge 全量 |
| `/profiles/ysc_single_agg.json` | 单仓：影视仓聚合 |
| `/profiles/hebi_vod.json` | 单仓：hebijunge 纯点播 |
| `/status.json` | 各上游抓取状态 |
| `/` | 状态看板（网页） |

---

## 订阅地址是怎么生成的

`scripts/merge.py` 的 `build_profiles()`：

1. 第一个条目固定是 `/tv.json`（全量聚合）
2. 其余条目是各上游经**与主配置完全相同的清洗**后独立落盘的`profiles/<id>.json`
3. 每个 profile 都经过 `validate.py` 校验，坏的不允许发布

订阅地址本身在 `validate.py::validate_subscription()` 里校验格式，
确保每条 URL 都能打开。

---

## 排错

**App 里刷新不出列表？**
1. 先在浏览器打开 `https://tv.bearno1.dpdns.org/subscriptions.json` 看能否正常显示
2. 能显示但 App 读不到 → 说明你的 App 不支持多仓订阅，改填 `/tv.json`
3. 全都打不开 → 换 Worker 备用地址

**某个仓打开是空的？**
不太可能——构建时校验不过就不会发布。可以看 `https://tv.bearno1.dpdns.org/status.json`
里各上游的 `status` 字段。

---

## 更新频率

上游每天两次（北京 10:05 / 22:05）更新 → Actions 提交 → Cloudflare 约 1 分钟内跟上。
单个上游抓取失败会沿用上次成功结果，并在 `status.json` 标记 `stale`。