#!/usr/bin/env bash
# ==============================================================================
# publish.sh —— 把本地已验证的 tv-hub 一键发布到 GitHub
#
# 用法（在你手工建好空仓库之后）：
#   bash publish.sh                      # 交互式询问仓库名
#   bash publish.sh bearnozhang/tv-hub   # 直接指定
#   bash publish.sh bearnozhang/tv-hub --private
#
# 前置条件（只需做一次）：
#   在 https://github.com/new 手工创建一个【空】仓库（不要勾选 README/.gitignore）
#   然后执行本脚本。
#
# 说明：本机 gh 已登录，但当前 token（fine-grained）缺少「创建仓库」与「Actions」
#       权限，所以创建仓库这一步必须你手工完成；本脚本负责其余全部环节。
# ==============================================================================
set -euo pipefail

cd "$(dirname "$0")"

REPO_ARG="${1:-}"
PRIVACY="${2:---private}"

if [ -z "$REPO_ARG" ]; then
  echo "请输入目标仓库（格式 用户名/仓库名）："
  read -r REPO_ARG
fi
if [ -z "$REPO_ARG" ]; then
  echo "错误：未指定仓库名。"; exit 1
fi

echo "目标仓库：$REPO_ARG  可见性：$PRIVACY"
echo

# ---- 1. 跑测试（不通过不发布） ------------------------------------------------
# 注意：`| tail` 会让管道退出码变成 tail 的 0，掩盖前面的失败。
# 必须开pipefail 且显式校验 ${PIPESTATUS[0]}，否则测试挂了也会继续发布。
echo "[1/5] 运行测试…"
set +e
python -m unittest discover -s tests 2>&1 | tail -3
_rc=${PIPESTATUS[0]}
set -e
if [ "$_rc" -ne 0 ]; then
  echo "❌ 测试未通过（退出码 $_rc），已中止，未推送任何内容。"
  exit 1
fi
echo "  ✅ 测试通过"
echo

# ---- 2. 跑构建（不通过不发布） ------------------------------------------------
echo "[2/5] 运行构建…"
set +e
python scripts/build.py 2>&1 | tail -4
_rc=${PIPESTATUS[0]}
set -e
if [ "$_rc" -ne 0 ]; then
  echo "❌ 构建失败（退出码 $_rc），已中止，未推送任何内容。"
  exit 1
fi
echo "  ✅ 构建通过"
echo

# ---- 3. 独立校验 ---------------------------------------------------------------
echo "[3/5] 校验产物…"
set +e
python scripts/validate.py --scope output 2>&1 | tail -5
_rc=${PIPESTATUS[0]}
set -e
if [ "$_rc" -ne 0 ]; then
  echo "❌ 产物校验失败（退出码 $_rc），已中止，未推送任何内容。"
  exit 1
fi
echo "  ✅ 校验通过"
echo

# ---- 4. 提交 -------------------------------------------------------------------
echo "[4/5] 提交变更…"
git add -A
if git diff --staged --quiet; then
  echo "  无变化，跳过提交。"
else
  git commit -q -m "chore: 更新聚合配置（sites=$(python -c "import json;print(json.load(open('public/status.json',encoding='utf-8'))['summary']['unique_sites'])")）"
  echo "  已提交：$(git log --oneline -1)"
fi
echo

# ---- 5. 配置远端并推送 ---------------------------------------------------------
echo "[5/5] 配置远端并推送…"
REMOTE_URL="https://github.com/$REPO_ARG.git"

# 安全闸：远端若已有提交，先确认是否可安全快进，绝不 --force 覆盖。
if git ls-remote --heads "$REMOTE_URL" main 2>/dev/null | grep -q .; then
  _remote_sha=$(git ls-remote "$REMOTE_URL" main | cut -f1)
  if git cat-file -e "${_remote_sha}^{commit}" 2>/dev/null; then
    if git merge-base --is-ancestor "$_remote_sha" HEAD; then
      echo "  远端 main 是本地祖先，快进推送安全。"
    else
      echo "❌ 安全闸：远端 main ($_remote_sha) 含本地没有的提交。"
      echo"   本脚本不会 --force 覆盖。已中止。"
      echo "   请先人工确认：git fetch origin && git log --oneline main..origin/main"
      exit 1
    fi
  else
    echo "  远端 main($_remote_sha) 本地无该对象（浅克隆或 force-push 过）。"
    echo "  为安全起见中止，请人工确认后手动 push。"
    exit 1
  fi
else
  echo "  远端无 main 分支（空仓库），直接推送。"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi
git push -u origin main
echo

echo "=============================================================="
echo "✅ 推送完成"
echo "=============================================================="
echo "仓库地址： https://github.com/$REPO_ARG"
echo
echo "接下来只需做两件事（都在网页上，各30 秒）："
echo
echo "1) 开启 Actions 自动运行"
echo "   https://github.com/$REPO_ARG/actions"
echo "   找到 update 工作流 → 点I understand, enable my workflows"
echo "   （若提示 workflow 未启用，先随便点一次 Run workflow 即可）"
echo
echo "2) 验证定时任务"
echo "   同样在 Actions 页面点update → Run workflow → 等2~3 分钟"
echo "   看到绿色√ 即成功。"
echo
echo "配置订阅地址（部署后即可用）："
echo "  https://raw.githubusercontent.com/$REPO_ARG/main/public/tv.json"
echo "  https://raw.githubusercontent.com/$REPO_ARG/main/public/live.json"
echo "  https://raw.githubusercontent.com/$REPO_ARG/main/public/status.json"