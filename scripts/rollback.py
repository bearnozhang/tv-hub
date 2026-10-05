#!/usr/bin/env python3
"""rollback.py —— 把 public/ 恢复到任一历史版本。

## 为什么用 git 历史而不是额外快照

产物本来是提交进仓库的，**git 历史本身就是最完整的快照**：
每一次自动更新都留下一个 commit，包含当时完整的 public/。
再另存一套快照只会占空间、并带来"快照和 git 不一致"的新问题。

所以回滚 = 从历史 commit 里取出 public/ 覆盖当前工作区。

## 用法

    python scripts/rollback.py --list                  # 看最近的历史版本
    python scripts/rollback.py --to <sha|ref>          # 恢复到该版本（不提交）
    python scripts/rollback.py --to <sha> --push       # 恢复并提交推送
    python scripts/rollback.py --last-healthy          # 回滚到最近一个质量合格的版本

## 判定「健康版本」

历史 commit 里的 `public/quality.json` 由 gate.py 写出，含 `passed` 字段。
`--last-healthy` 会从新到旧找第一个 `passed=true` 的版本。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", check=False) if not check else \
        subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", check=True)


def history(limit: int = 15) -> list[dict]:
    """列出改动过 public/ 的最近提交。"""
    r = subprocess.run(
        ["git", "log", f"-n{limit}", "--format=%H|%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M",
         "--", "public/"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            out.append({"sha": parts[0], "short": parts[1],
                        "date": parts[2], "subject": parts[3]})
    return out


def quality_of(sha: str) -> dict | None:
    """读取某次提交里的 public/quality.json。"""
    r = subprocess.run(["git", "show", f"{sha}:public/quality.json"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return None


def do_list(limit: int) -> int:
    rows = history(limit)
    if not rows:
        print("没有找到改动 public/ 的历史提交。")
        return 0
    print(f"{'#':>2}  {'提交':9s} {'时间':17s} {'质量':6s} 说明")
    print("-" * 86)
    for i, h in enumerate(rows):
        q = quality_of(h["sha"])
        if q is None:
            badge = "—"
        else:
            badge = "✅通过" if q.get("passed") else "❌未过"
        subj = h["subject"][:44]
        print(f"{i:>2}  {h['short']:9s} {h['date']:17s} {badge:6s} {subj}")
    print()
    print("恢复命令：python scripts/rollback.py --to <提交> [--push]")
    print("回滚到最近合格版本：python scripts/rollback.py --last-healthy --push")
    return 0


def do_rollback(ref: str, push: bool, dry_run: bool) -> int:
    r = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print(f"❌ 找不到提交：{ref}")
        return 2
    sha = r.stdout.strip()
    print(f"目标版本：{sha[:8]}")

    q = quality_of(sha)
    if q:
        print(f"  该版本质量：{'✅ 通过' if q.get('passed') else '❌ 未通过'} "
              f"（检查于 {q.get('checked_at_bj')}）")
        if q.get("hard_issues"):
            for h in q["hard_issues"][:3]:
                print(f"    ⚠ {h}")

    if dry_run:
        print("  --dry-run：只显示将要执行的操作，未改动文件")
        return 0

    subprocess.run(["git", "checkout", sha, "--", "public/"], cwd=ROOT, check=True)
    print("  ✅ public/ 已恢复到该版本")

    if push:
        msg = f"revert: 回滚 public/ 到 {sha[:8]}（应对线上异常）"
        subprocess.run(["git", "add", "public"], cwd=ROOT, check=True)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if r.returncode == 0:
            print("  无变化，跳过提交")
            return 0
        subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, check=True)
        subprocess.run(["git", "push"], cwd=ROOT, check=True)
        print("  ✅ 已提交并推送（Cloudflare 将自动重新部署）")
    else:
        print("  下一步（确认无误后执行）：")
        print(f"    git add public && git commit -m 'revert: 回滚 public/ 到 {sha[:8]}' && git push")
    return 0


def do_last_healthy(push: bool, dry_run: bool, limit: int) -> int:
    for h in history(limit):
        q = quality_of(h["sha"])
        if q and q.get("passed"):
            print(f"找到最近合格版本：{h['short']}  {h['date']}  {h['subject'][:50]}")
            return do_rollback(h["sha"], push, dry_run)
    print("❌ 最近的提交里没有标记为「质量通过」的版本")
    print("   （quality.json 从本次架构升级后才开始生成，早于它的版本没有标记）")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="tv-hub 产物回滚")
    ap.add_argument("--list", action="store_true", help="列出历史版本")
    ap.add_argument("--to", help="恢复到指定 commit")
    ap.add_argument("--last-healthy", action="store_true", help="回滚到最近一个质量合格版本")
    ap.add_argument("--push", action="store_true", help="回滚后自动提交并推送")
    ap.add_argument("--dry-run", action="store_true", help="只显示，不改动")
    ap.add_argument("--limit", type=int, default=15)
    a = ap.parse_args()

    if a.list or (not a.to and not a.last_healthy):
        return do_list(a.limit)
    if a.to:
        return do_rollback(a.to, a.push, a.dry_run)
    return do_last_healthy(a.push, a.dry_run, a.limit)


if __name__ == "__main__":
    raise SystemExit(main())
