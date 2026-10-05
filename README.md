# tv-hub

TVBox / 影视仓配置聚合器。**多上游自动抓取 → 实际校验 → 去重 → 合并 → 失败回退 → 统一配置输出**，
由 GitHub Actions 每天两次（北京时间 10:05 / 22:05）自动更新并回提交。

纯标准库 Python，无第三方依赖、无Docker、无数据库、无 Redis、无 LLM。

---

## 快速开始

```bash
# 完整流程（抓取 → 校验 → 合并 → 产出）
python scripts/build.py

# 只用现有缓存重建
python scripts/build.py --skip-fetch

# 单独跑各阶段
python scripts/fetch.py                    # 抓取，写 cache/
python scripts/fetch.py --id hebi_tvbox    # 只抓指定上游
python scripts/validate.py --scope cache   # 校验缓存
python scripts/validate.py                 # 校验 public/ 产物
python scripts/merge.py --stats-only       # 只看去重统计

# 跑测试（90 个用例，含契约/门禁/巡检的架构测试）
python -m unittest discover -s tests -v
```

产物输出到 `public/`：

| 文件 | 说明 |
| --- | --- |
| `tv.json` | **主产物**，TVBox / 影视仓「配置地址」直接填它 |
| `live.json` | 直播配置（按分组展开，同名频道多备源聚合） |
| `subscriptions.json` | 汇总的多仓订阅清单 |
| `status.json` | 机器可读状态：每个上游的成功/失败/数量/错误 |
| `index.html` | 人看的状态看板 |

---

## 增删上游：只改 `config/sources.json`

这是**唯一**的上游配置入口，其余代码无需改动。

```jsonc
{
  "id": "hebi_tvbox",              // 唯一标识，缓存文件名用它
  "name": "hebijunge·TVBox 全量",  // 展示名
  "type": "tvbox",// tvbox | live_txt | live_m3u | subscription
  "repo": "hebijunge/tvbox-config",
  "branch": "main",
  "path": "tvbox.json",            // 仅作记录，方便溯源
  "enabled": true,                 // false = 临时停用，不删配置
  "primary":  "https://raw.githubusercontent.com/...",   // 主地址
  "fallback": ["https://cdn.jsdelivr.net/gh/..."],       // 备用，按序尝试
  "notes": "实测规模与坑位"
}
```

`defaults` 里可调 `timeout` / `retries` / `retry_backoff_seconds` / `user_agent`。

配置非法会**直接报错拒绝运行**（缺字段、type 未知、id 重复、primary 非 http(s) 等），
不会静默跑出错误结果。

---

## 架构与质量保障

完整设计见 **[ARCHITECTURE.md](ARCHITECTURE.md)**。核心是六层 + 两套自动化机制：

| 层 | 文件 | 职责 |
|---|---|---|
| L0 契约 | `contract/kernel.json` + `scripts/kernel.py` | 客户端 DTO 契约的**唯一真源**（字段类型/必填/URL 规则） |
| L1 摄取 | `scripts/fetch.py` | primary → fallback → 缓存回退 |
| L2 收敛 | `scripts/merge.py` + `scripts/sanitize.py` | URL 清洗、契约对齐、去重、分档 |
| L3 门禁 | `scripts/gate.py` | **决定能不能发布**（不达标 → 拒绝发布，线上保持旧版） |
| L4 发布 | `scripts/rollback.py` | 产物落到 `public/`，可回滚到任一历史版本 |
| L5 分发 | `worker-no-compress.js` | Cloudflare Worker：路径别名 + 强制 identity 编码 |
| L6 巡检 | `scripts/watchdog.py` + `watchdog.yml` | 每 6 小时从站外验证线上，异常开 Issue，超时未更新自动补触发 |

**两道自动化防线：**

1. **发布前拦坏东西**（`gate.py`）：契约冲突=0、站点数未骤降、关键分组存在。
   不通过 → CI 不提交 → 线上不受影响。
2. **发布后盯住它**（`watchdog.py`）：从用户视角拉线上产物逐项验证。
   异常开 GitHub Issue，内容超 30 小时未更新会自动补触发构建。

**常用运维命令：**

```bash
python scripts/kernel.py --describe     # 查看当前客户端契约
python scripts/gate.py                  # 本地跑一次质量门禁
python scripts/watchdog.py              # 巡检线上
python scripts/rollback.py --list       # 列出可回滚的历史版本
python scripts/rollback.py --last-healthy --push   # 回滚到最近一个合格版本
```

## 设计要点

### 1. 结构识别：不粗暴拼接

先判形态再处理，四类分开走：

| type | 判定 | 处理方式 |
| --- | --- | --- |
| `tvbox` | dict 且含 `sites`/`parses`/`lives`/`flags`/`doh`/`rules` | 按字段语义合并 |
| `live_txt` | `名称,URL` 文本，分组 `名称,#genre#` | 解析成 channels → 按 group 聚合 |
| `live_m3u` | `#EXTINF` + URL | 同上，并取 `tvg-id`/`tvg-logo` |
| `subscription` | `{"urls":[...]}` | 只产出订阅清单，**不**参与 sites 合并 |

不同结构的 JSON 不会被塞进同一个数组。`spider` / `wallpaper` / `doh` / `rules` 等标量
按「先到先得」保留，不做覆盖式合并。

### 2. 去重：按稳定字段，不按来源 URL

身份键按优先级取第一个命中的：

1. `key:X` — 最强，同key 就是同一站点
2. `name+api`（归一化：去空白/尾斜杠/大小写）
3. `name + type`
4. 内容哈希（`key/name/type/api/ext/jar/playUrl/categories`）

因此**两个来源 URL 不同但内容完全相同的站点会被合并**。
实测：`hebi_tvbox` 与 `hebi_vod` 各 4331 个站点，去重后仅净增 3 个。

- `parses` / `lives`（对象型）→ 按 `name`
- `flags` → **实测是字符串数组**（`["youku","优酷"]`），按字符串去重
- 直播频道 → `name` 聚合多备源，`url` 精确去重

### 3. 上游脏数据的处理

实测发现并已修复：

| 问题 | 处理 |
| --- | --- |
| `lives` 里混入站点对象（带 `key`/`api`/`jar`） | 分流回 `sites`，不污染 lives |
| `lives` 条目 `url: ""` 空占位 | 丢弃 |
| `site.type` 为字符串 `"3"` | 归一化为 int |
| `ext` 为 `list` / `null` / `{}` | list→dict，null/空→剔除 |
| TXT 分组写作 `名称,#genre#`（`#` 不在行首） | 正则识别，行首行尾都认 |
| `🕘️更新时间` 伪分组 | 归一化掉 |
| 各源分组命名不一致 | `norm_group()` 归一 + 同义映射 |

### 4. 失败回退：不清空旧数据

单个上游抓取失败时：

- 保留 `cache/<id>.json` **原文件不动**
- `status` = `stale`，`from_cache` = true
- `last_success` 保持上一次成功时间
- `consecutive_failures` +1
- `error` 记录完整失败原因（含每个备用 URL 的尝试结果）

**硬性底线**：若所有上游都失败且本地无历史成功缓存，`build.py` 以退出码 `3` 失败，
**绝不生成一个看起来正常但实际为空的 tv.json**。

### 5. status.json

每个上游都记录：`status` / `last_attempt` / `last_success` / `consecutive_failures` /
`used_url`（实际生效的地址）/ `http_status` / `bytes` / `sha256` / `counts` /
`from_cache` / `error` / `warnings`。

---

## GitHub Actions

`.github/workflows/update.yml`：

- **每天北京时间 10:05 与 22:05** → cron `5 2 * * *` 与 `5 14 * * *`（Actions 用 UTC，
  UTC = 北京时间减 8 小时；刻意避开整点以减少排队延迟）
- 支持 `workflow_dispatch` 手动触发，可勾选「只用缓存重建」
- 流程：lint 自检 → 测试 → build → 独立复核 → 写摘要 → **有变化才commit/push** → 传Artifact
- 权限最小化：仅 `contents: write`
- `concurrency` 防并发重叠

配置可选变量 `TVHUB_BASE_URL`，用于让 README/看板里的订阅地址显示为你的实际域名。

### ⚠️ 关于 push 触发

GitHub 规则：**workflow 在默认分支上失败后，push 触发器会被抑制**（最长 60 天），
期间仍可正常 cron 与手动触发。本项目2026-10-04 的 run#1 就是缩进错误导致的失败
（症状：`jobs=[]`、`created_at == updated_at`、网页无任何步骤日志）。

修复后：
- run#2（`workflow_dispatch`）✅ success，9 个步骤全绿
- 自动提交 `chore: 自动更新配置 <run_id>` 由 `github-actions[bot]` 完成
- push 触发仍处抑制期 → 依赖 cron（北京时间 10:05 / 22:05）维持日常更新

**`scripts/workflow_lint.py` 就是为此写的**：它能在提交前抓出这类静默失败。

---

## 本地验证过的结果（2026-10-04 实跑）

```
上游           8/8 全部抓取成功（HTTP 200）
raw_sites      8837  →  unique 4334（去重 4503，50.9%）
parses         421   →  unique 222
lives          595   →  unique 572  +  37 个直播分组 / 3765 频道
flags          60
测试           43 passed
产物校验tv.json / live.json / status.json 全部 OK
```

**GitHub Actions 实跑**：run#2 成功，9 个步骤全绿，耗时 23s，
自动提交 `9f2994c chore: 自动更新配置 37179833144（sites=4334 去重=4503）`，
badge = `passing`。

失败回退经实测验证：把 `hebi_tvbox` 主/备地址改成不可解析域名后，
`status` 变`stale`、`from_cache=true`、`last_success` 不变、`consecutive_failures` 0→1、
缓存文件 1,654,422 B 完好、`sites=4331` 一致。

---

## 后续

- Cloudflare 已通过 **Worker** 接入（自定义域 `tv.bearno1.dpdns.org`），
  由 `deploy.yml` 在产物变更后自动部署，**不是** Pages。
- 待办见 `ARCHITECTURE.md` 的「落地清单 · 下一批」：
  产物矩阵收敛、`tv-mini` 档位、客户端能力表、上游变更感知。

## 数据来源与免责

所有配置均来自互联网公开分享的第三方仓库，**版权归原作者所有**。本项目仅做聚合、校验、
去重与镜像，不修改任何第三方上游仓库，不用于商业用途。
上游可用性不由本项目保证，请自行评估。