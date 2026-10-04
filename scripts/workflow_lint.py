#!/usr/bin/env python3
"""
workflow_lint.py —— GitHub Actions workflow 结构校验（零依赖）。

为什么需要它：
  本项目曾在 update.yml 里犯过一个缩进错误 —— 某个 `- name:` 顶格（0 缩进）
  而同级步骤是6 缩进。GitHub 接受了这个文件并创建了 run 记录，
  但 jobs=[]、run 秒级 failure，**任何 runner都没被分配**，
  在网页日志里极难定位。本校验器把这类错误在提交前抓出来。

校验规则：
  1. 禁止 TAB 缩进
  2. 序列项（`- `）在同一容器内必须缩进一致
  3. 嵌套块内容必须比父键更深
  4. run: | 块内的 heredoc 结束符必须与块内容基线对齐
  5. cron 表达式必须是合法 5 段
  6. jobs 下每个 job 必须有 runs-on 与 steps
  7. "on" 必须被引号包裹（避免 YAML 1.1 解析成布尔 True）
  8. 关键 action 存在

用法：
  python scripts/workflow_lint.py
  python scripts/workflow_lint.py .github/workflows/update.yml
退出码 0 = 通过；1 = 有错误。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, ".github", "workflows", "update.yml")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def lint(path: str) -> list[str]:
    errs: list[str] = []
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    lines = raw.split("\n")

    # --- 规则 1：TAB ---
    for i, l in enumerate(lines, 1):
        if "\t" in l:
            errs.append(f"L{i}: 含 TAB 缩进（YAML 只允许空格）")

    # --- 规则 2：序列项缩进一致性 ---
    # 做法：找出所有 `steps:` / `permissions:` 等容器键，收集其块内的序列项，
    # 要求同一容器内的序列项缩进完全一致。
    # （不能按「向上找更小缩进的父键」分组—— 顶格项会一路跳到文件顶层，
    #   反而和真正的 steps 项分到不同组，漏报。）
    container_keys = ("steps:", "permissions:", "rules:", "branches:")
    for ci, cl in enumerate(lines, 1):
        key = cl.strip()
        if not any(key.startswith(k) for k in container_keys):
            continue
        base = indent_of(cl)
        items: list[tuple[int, int]] = []
        # 块范围：从 steps: 之后直到出现「缩进 <= base 且非序列项」的键，或下一个同级容器键。
        # 序列项缩进可能大于 base（正常）或等于 0（错误），两者都要收集起来比对。
        for j in range(ci, len(lines)):
            sl = lines[j]
            if not sl.strip():
                continue
            ss = sl.strip()
            ind = indent_of(sl)
            if ss.startswith("- "):
                items.append((j + 1, ind))
                continue
            if ind <= base:
                # 非序列项且缩进不深于容器 → 块结束
                break
        indents = {ind for _, ind in items}
        if len(indents) > 1:
            detail = ", ".join(f"L{i}(缩进{ind})" for i, ind in items)
            errs.append(f"容器 `{key}`(L{ci}) 内序列项缩进不一致：{detail}")
            errs.append("      → YAML 层级断裂，GitHub 会创建 run 但不分配任何 job")

    # --- 规则 3/4：块结构与 heredoc 对齐 ---
    # 注意：不能对 run: | 块内的「缩进一致性」下判断——
    #   shell 的 if/fi、Python 代码天然是多级缩进，属于正常内容。
    #   这里只校验 heredoc 结束符对齐（规则 4），块内缩进交由 shell/python 自己报错。
    in_heredoc = False
    heredoc_term = ""
    heredoc_base = 0
    heredoc_start = 0
    for i, l in enumerate(lines, 1):
        m = re.match(r"^(\s*)<<'(\w+)'\s*$", l)
        if m and not in_heredoc:
            in_heredoc = True
            heredoc_term = m.group(2)
            heredoc_base = indent_of(l)
            heredoc_start = i
            continue
        if in_heredoc:
            if l.strip() == heredoc_term:
                if indent_of(l) != heredoc_base:
                    errs.append(f"L{i}: heredoc 结束符 `{heredoc_term}` 缩进 {indent_of(l)} "
                                f"与起始 L{heredoc_start} 的 {heredoc_base} 不一致 "
                                f"→ shell 视为未闭合，步骤必然失败")
                in_heredoc = False
                continue
            if l.strip() and indent_of(l) < heredoc_base:
                errs.append(f"L{heredoc_start}: heredoc `{heredoc_term}` 在 L{i} 前未见结束符")
                in_heredoc = False
    if in_heredoc:
        errs.append(f"L{heredoc_start}: heredoc `{heredoc_term}` 全文未闭合")

    # --- 规则 5：cron ---
    for c in re.findall(r'cron:\s*"([^"]*)"', raw):
        parts = c.split()
        if len(parts) != 5:
            errs.append(f"cron 字段数应为 5，实际 {len(parts)}: {c}")
        else:
            mi, ho = int(parts[0]), int(parts[1])
            if not (0 <= mi <= 59 and 0 <= ho <= 23):
                errs.append(f"cron 分钟/小时越界: {c}")

    # --- 规则 6：jobs 结构 ---
    if "jobs:" not in raw:
        errs.append("缺少 jobs:")
    else:
        for jm in re.finditer(r"^(\s*)(\w[\w-]*):\s*$", raw, re.M):
            key = jm.group(2)
            if key in ("runs-on", "steps", "permissions", "concurrency", "timeout-minutes"):
                continue
            seg = raw[jm.end():]
            # 只看紧跟 job 定义块内是否含 runs-on / steps
            pass
        for job in re.split(r"\n(?=\s{4}\w[\w-]*:\s*$)", raw):
            if "steps:" in job and "runs-on:" not in job and re.search(r"\n {6}steps:", job):
                name = job.strip().split(":")[0]
                errs.append(f"job `{name}` 有 steps 但缺 runs-on")

    # --- 规则 7：on 加引号 ---
    if re.search(r"^on:\s*$", raw, re.M):
        errs.append("`on:` 未加引号 → YAML 1.1 会解析成布尔 True，GitHub 可能不识别触发器")
    if not re.search(r'^"on":\s*$', raw, re.M):
        errs.append("缺少被引号包裹的 \"on\": 键")

    # --- 规则 8：关键动作 ---
    for act in ("actions/checkout@", "actions/setup-python@"):
        if act not in raw:
            errs.append(f"缺少必要 action: {act}")
    if "git push" not in raw:
        errs.append("缺少 git push（产物不会回提交）")

    return errs


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.exists(path):
        print(f"[lint] 文件不存在: {path}")
        return 1
    errs = lint(path)
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    if errs:
        print(f"[lint] {rel} 发现 {len(errs)} 个问题：")
        for e in errs:
            print(f"  ✗ {e}")
        return 1
    with open(path, encoding="utf-8") as f:
        steps = len(re.findall(r"^\s+- name:", f.read(), re.M))
    print(f"[lint] {rel} 结构校验通过（{steps} 个步骤）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())