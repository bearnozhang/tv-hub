# UPSTREAM-RESEARCH · 上游实测调研

> 全部结论来自 **2026-10-04 实际执行**：`gh api` 读仓库元数据与文件树 + `curl` 真实下载并解析 JSON。
> 不依赖 README 描述，所有 URL 均经过实际 HTTP 下载验证。
> 复现命令见文末「调研复现命令」。

---

## 一、仓库总览（gh api 实测）

| 仓库 | default_branch | has_pages | pushed_at (UTC) | 文件数 | 体积 |
| --- | --- | --- | --- | --- | --- |
| `Lightconer/tvbox-ysc-config` | **main** | false | 2026-10-03T17:49:35Z | 14 | 46 KB |
| `hebijunge/tvbox-config` | **main** | true | 2026-10-04T02:47:11Z | 10872 | ~450 MB |
| `Lightconer/TVBox-Sources` | **main** | false | 2026-10-04T03:17:27Z | 18 | 542 KB |

> **注意**：三个仓库的默认分支都是 `main`，**不是 `master`**。raw 地址里写错分支会 404。

### 最近 commit

| 仓库 | SHA | 时间(UTC) | 消息 |
| --- | --- | --- | --- |
| tvbox-ysc-config | `749fb1cb` | 2026-10-03T17:49:34Z | chore: 自动更新影视仓配置 |
| hebijunge/tvbox-config | `1d3b702a` | 2026-10-04T02:47:10Z | chore: 更新 CI 指标基线 |
| TVBox-Sources | `0a78e24b` | 2026-10-04T03:17:26Z | chore: 自动更新直播源 |

三个仓库都是**高频自动更新**（上游自身也有 Actions），适合做聚合源。

---

## 二、逐仓库实测结构

### 2.1 Lightconer/tvbox-ysc-config

真实文件树（`gh api repos/…/git/trees/main?recursive=1`）：

```
config/sources.json          2556 B   ← 上游自己的源清单
scripts/update.py           14907 B
output/4k.json              34026 B
output/feimao.json          16321 B
output/ouge.json            11737 B
output/shield.json            100 B
output/status.json           2892 B
output/wangerxiao.json      19065 B
output/单仓聚合.json68815 B   ← tvbox 结构，含 sites
output/多仓订阅.json           576 B   ← {"urls":[...]}，不是 tvbox
.github/workflows/update.yml  1743 B
```

**实测下载解析结果**：

| 文件 | HTTP | 字节 | 顶层 | 关键字段 |
| --- | --- | --- | --- | --- |
| `output/单仓聚合.json` | 200 | 68,815 | dict | `sites`=167 `lives`=7 `parses`=33 `flags`=61 `doh`=16 `rules`=33 `spider`✓ |
| `output/多仓订阅.json` | 200 | 576 | dict | 仅 `urls`（4 条）→ **结构不同，不能当 sites 合并** |

**raw 地址（已验证 200）**：
```
https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/单仓聚合.json
  → 实测需 URL 编码：output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json
https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/多仓订阅.json
  → 实测需 URL 编码：output/%E5%A4%9A%E4%BB%93%E8%AE%A2%E9%98%85.json
```

**上游自己的 status.json 快照**（证明上游也不稳定）：
```
成功 4/10：肥猫✅ 王二小✅ 4K小盒子✅ 讴歌📦缓存
失败 6/10：饭太硬/摸鱼/OK/小米/巧记/潇洒（DNS 解析失败为主）
```
→ 这直接印证了本项目「失败保留上次结果」设计的必要性。

### 2.2 hebijunge/tvbox-config

**10,872 个文件 / ~450 MB**，是本组中体量最大的。根目录实际产物：

```
tvbox.json     1,654,422 B   ← 主配置（sites+lives+parses）
vod.json       1,534,012 B   ← 纯点播（无 lives）
live.json            268 B   ← 只有 1 条 lives 引用
short.json      203,223 B
status.json     370,025 B
index.html       15,860 B
list.json          8,403 B   ← 15 条上游健康度清单
lives/live_all.txt 899,928 B   ← 直播全量（另有 lives/groups/*.m3u|txt）
exports/**                ← 大量派生导出
adult*.json               ← 成人内容，本项目【不采集】
```

**实测下载解析结果**：

| 文件 | HTTP | 字节 | sites | lives | parses |
| --- | --- | --- | --- | --- | --- |
| `tvbox.json` | 200 | 1,654,422 | **4331** | 586 | 194 |
| `vod.json` | 200 | 1,534,012 | 4331 | — | 194 |
| `live.json` | 200 | 268 | — | 1 | — |

**结构要点（实测踩坑，必须知道）**：
1. `lives` 数组里**混入了站点对象**（带 `key`/`api`/`jar`，`type:3`），例如
   `{"name":"咖啡体育","type":3,"api":"csp_KafLiveAmns","playerType":2}` —— 这是上游数据缺陷，
   正确处理是**分流成 site**，不能原样塞进 `lives`。
2. 部分 `lives` 条目 `url: ""`（空串占位）。
3. `ext` 字段有三种形态：`str`（JSON 串）、`dict`（`{k:url}`）、`list`（`[{name,url}]`，非TVBox 标准）。
4. `site.type` 存在**字符串型** `"3"`，需归一化为 int。
5. 仓库 `has_pages: true`，`pages.yml` 会把 `tvbox.json`/`vod.json`/`live.json` 等拷进站点目录。

**raw 地址（已验证 200）**：
```
https://raw.githubusercontent.com/hebijunge/tvbox-config/main/tvbox.json
https://raw.githubusercontent.com/hebijunge/tvbox-config/main/vod.json
https://raw.githubusercontent.com/hebijunge/tvbox-config/main/lives/live_all.txt
```

**`live_all.txt` 实测**：639,233 B / 8,167 行 → 解析出 **8,125 个频道 / 35 个分组**
（轮播2764、卫视992、其他827、央视 773、地方-浙江 393…）

### 2.3 Lightconer/TVBox-Sources

```
config/config.yaml           8620 B
scripts/{run,crawl,check,output,utils,diag,group_map}.py
output/live.txt             14743 B   ← 直播 TXT（189 行）
output/live.m3u             35423 B   ← 直播 M3U（364 行，含 tvg-logo/tvg-id）
output/tvbox.json376 B   ← 直播引用壳
output/status.json513 B
output/badge.json129 B
```

**实测下载解析结果**：

| 文件 | HTTP | 字节 | 结果 |
| --- | --- | --- | --- |
| `output/tvbox.json` | 200 | 378 | `sites`=[] `lives`=2 `spider`="" → **纯直播壳，不贡献站点** |
| `output/live.txt` | 200 | 14,743 | 189 行 → **172 频道 / 17 组** |
| `output/live.m3u` | 200 | 35,423 | 364 行 → **172 频道 / 17 组**（与 txt 同源同频道） |

**关键格式发现（实测踩坑）**：TXT 的分组行写作 **`名称,#genre#`**，`#` **不在行首**：

```
🕘️更新时间,#genre#                      ← 这是分组（伪分组，内容是时间戳）
2026-10-04 03:16:34,http://38.75.136.137:98/gslb/dsdqbv/cctv10hd.m3u8?auth=test20251009
📺央视频道,#genre#                        ← 这是分组
CCTV-10 (720p),http://38.75.136.137:98/...
CCTV-10 (720p),http://74.91.26.218:82/live/cctv10hd.m3u8   ← 同名频道多备源
```

> 只按 `line.startswith("#")` 判分组的实现会**漏掉全部17 个分组**。
> 本项目实现用 `re.search(r"#\s*genre\s*#")`，行首行尾两种写法都能认。
> 另：`🕘️更新时间` 是上游塞时间戳的伪分组，需归一化掉，否则会凭空多出一个分组。

**跨源分组对齐**：`央视频道`↔`央视`、`卫视频道`↔`卫视`、`☘️浙江频道`↔`地方-浙江`，
靠 `norm_group()` 归一化后，TXT 与 M3U 两路、以及与 hebijunge 的分组才能合并成同一组
（实测对齐结果：`tvs_live_txt` 与 `tvs_live_m3u` 分组分布完全一致）。

---

## 三、最终采用的 8 个上游（全部实测 HTTP 200）

| # | id | 类型 | 主URL（raw） | 实测规模 |
| --- | --- | --- | --- | --- |
| 1 | `ysc_single_agg` | tvbox | `…/tvbox-ysc-config/main/output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json` | sites 167 / parses 33 / lives 7 |
| 2 | `ysc_multi_sub` | subscription | `…/output/%E5%A4%9A%E4%BB%93%E8%AE%A2%E9%98%85.json` | urls 4 |
| 3 | `hebi_tvbox` | tvbox | `…/hebijunge/tvbox-config/main/tvbox.json` | sites 4331 / parses 194 / lives 586 |
| 4 | `hebi_vod` | tvbox | `…/main/vod.json` | sites 4331 / parses 194 |
| 5 | `tvs_live_txt` | live_txt | `…/TVBox-Sources/main/output/live.txt` | 172 频道 / 17 组 |
| 6 | `tvs_live_m3u` | live_m3u | `…/main/output/live.m3u` | 172 频道 / 17 组 |
| 7 | `hebi_live_all` | live_txt | `…/main/lives/live_all.txt` | 8125 频道 / 35 组 |
| 8 | `tvs_tvbox_stub` | tvbox | `…/main/output/tvbox.json` | sites 0 / lives 2（直播壳） |

每个都配了 **jsdelivr / ghfast** 备用地址（`fallback` 字段），主地址失效时自动切换。

---

## 四、被排除的内容及原因

| 路径 | 排除原因 |
| --- | --- |
| `hebijunge` 的 `adult*.json`、`adult_live*`、`deps/**/线上看女优.json` 等 | 成人内容，不采集 |
| `hebijunge` 的 `exports/**`（`all.json` 2.3MB 等） | 上游自己的派生导出，采信根目录主产物即可，避免重复 |
| `ysc-config` 的 `feimao/ouge/4k.json` 等单仓文件 | 已包含在 `单仓聚合.json` 里，重复采集无意义 |
| 上游 README 中提及的 `ghproxy` 等第三方加速前缀 | 未实测稳定性，不写进默认 fallback 的第一优先 |

---

## 五、合并后实际结果（2026-10-04 实跑）

```
raw_sites              8837   ← 各源 sites 相加 + 从 lives 救回 8 个
unique_sites           4334
sites_deduped          4503   ← 去重率 50.9%
raw_parses421→ unique 222
raw_lives              595  → unique 572（+ 37 个自建直播分组）
live_channels          3765（另有313 条重复 URL 被去重）
unique_flags           60
subscriptions          4
```

去重生效验证：`hebi_tvbox`(4331) 与 `hebi_vod`(4331) 是同源同结构，
去重后仅净增 3 个站点 —— 证明去重是按`key` 判定的，**不是**因为 URL 不同就重复保留。

---

## 六、调研复现命令

```bash
# 仓库元数据（default_branch / pushed_at / has_pages）
for r in Lightconer/tvbox-ysc-config hebijunge/tvbox-config Lightconer/TVBox-Sources; do
  gh api "repos/$r" --jq '{full_name,default_branch,pushed_at,has_pages,size}'
done

# 真实文件树
gh api "repos/Lightconer/tvbox-ysc-config/git/trees/main?recursive=1" \
   --jq '.tree[]|select(.type=="blob")|"\(.size)\t\(.path)"'

# 真实下载并解析（不要猜 URL）
curl -s "https://raw.githubusercontent.com/hebijunge/tvbox-config/main/tvbox.json" | python -c "
import json,sys; d=json.load(sys.stdin)
print('sites',len(d.get('sites',[])),'parses',len(d.get('parses',[])),'lives',len(d.get('lives',[])))"
```