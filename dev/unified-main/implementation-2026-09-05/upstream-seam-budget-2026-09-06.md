# 上游同步后的接缝预算误报

在分支提交 `c3dab25437` 上，原检查报告
`desktop/macos/docs/desktop-updates.mdx` 增加 4 行、预算 1 行。但这 4 行来自
上游提交 `382fb3bae3` 对 serving-backend provenance 的原版说明。
与 `upstream/main` 直接对比，fork 只有原已允许的一行 Cloudflare staging
说明。核对时 `origin/main=d238a85af9`、`upstream/main=c4880cd5f6`。

此前的 `3495ef70d1` 已在同步流程中排除与上游逐字节相同的文件，但允许
接缝文件同时包含上游更新和原有 fork 修改时，行数仍从 PR diff 取值。
它还会漏掉此前已存在的 fork 修改，使分多次提交的接缝可能累计超预算。
两个现象来自同一个计数边界，不能通过扩大白名单或删掉上游说明解决。

现有检查现在仍用 `base...head` 选择本轮文件，但用
`git diff --numstat upstream/main HEAD -- <path>` 计算完整接缝差异。
不允许修改的文件类别、白名单、单文件预算、缺少上游引用的提示以及普通
fork 文件的判定保持原规则。没有修改该桌面文档或允许清单。

## 验证

- 两个新回归测试运行真实临时 Git 仓库、上游分支合并和生产检查 CLI。
  修改检查前均失败：上游新增四行被误算，已有三行再增加一行却被放过。
- 修改后 `backend/.venv/bin/python scripts/fork/test_check_upstream_touch.py`：
  **14 passed**。既有未允许文件、禁止类别、双重登记、超预算及缺少上游
  引用等用例继续通过。新用例保留合并后的上游原文，并拒绝累计超预算。
- 在实际分支运行 `backend/.venv/bin/python scripts/fork/check-upstream-touch.py
--json`：通过；唯一受预算检查的文件为该桌面说明，结果为 `+1/1`。
- 固定版本 Python 格式检查和 `git diff --check` 通过。代码与测试均属于
  fork 自有文件。新回归由已有 `fork-upstream-touch-tests` 本地/CI 门禁
  执行，没有增加独立检查或放宽任何预算。

此次复用现有 fork 差异检查所有者，未增加旁路或第二套接缝清单。真实事件
证据是上述本地检查结果和上游提交；原始记录保存在仓库外的
`/Users/macstudio/.codex/eddy-production/`：
`upstream-budget-regression-red-20260906.log`、
`upstream-budget-regression-green-20260906.log`、
`upstream-budget-actual-green-20260906.json`。

失败类为新登记的 `FC-fork-seam-budget-from-pr-diff`。这个检查通过只消除
错误的 fork 差异计数；Cloudflare 业务迁移、双目标验收、生产发布和 macOS
验收仍需继续，不代表整个发布门禁已经通过。
