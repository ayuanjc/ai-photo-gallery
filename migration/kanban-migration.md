# 看板迁移方案：GitHub/GitLab Issues → 飞书项目

> 目标平台：**飞书项目** ｜ 源：**GitHub/GitLab Issues** ｜ 范围：**全量**（工作项、历史、字段/标签、状态流转、自动化、成员权限）

## 1. 迁移概览

- **源数据**：GitHub/GitLab Issues（标题、描述、状态、标签、指派、里程碑）。
- **目标**：飞书项目看板（工作项 + 字段 + 状态流 + 标签 + 自动化 + 成员）。
- **角色分工**：后端开发、前端/客户端、测试/QA（以角色占位，导入后替换为具体成员）。

## 2. 数据源现状与获取方式

**重要**：当前本地仓库（`a01`）未关联任何 `git remote`，也没有 `.github` Issue 模板或可直接拉取的 Issue 数据（仅一个 initial commit）。因此迁移分两条路径：

| 路径 | 适用 | 做法 |
| --- | --- | --- |
| A. 有真实远程仓库 | 已在 GitHub/GitLab 建 Issues | 用 `migration/export_github_issues.py` 导出 CSV，再做字段映射导入飞书 |
| B. 无真实 Issues | 当前仅代码库、无 Issue 记录 | 直接采用 `migration/work_items.csv`（已按代码现状拆解的工作项） |

**导出真实 Issues**（路径 A）：

```bash
# GitHub
set GITHUB_TOKEN=<Personal Access Token>
python migration/export_github_issues.py github --owner <org/user> --repo <repo> --out issues.csv

# GitLab
set GITLAB_TOKEN=<Access Token>
python migration/export_github_issues.py gitlab --project <project_id或路径> --out issues.csv
```

## 3. 字段映射

| GitHub/GitLab 字段 | 飞书项目字段 | 说明 |
| --- | --- | --- |
| `title` | 工作项标题 | 直接映射 |
| `body` / `description` | 描述 | 保留 Markdown，飞书支持富文本 |
| `state`(open/closed) | 状态 | open→待办，closed→已完成（见 §4） |
| `labels` | 标签 | 逗号分隔，按 §5 规则规整 |
| `assignee` / `assignees` | 负责人 | `login/username` → 飞书成员（见 §6） |
| `milestone` | 迭代/里程碑 | 直接映射 |
| `type`(issue/PR) | 工作项类型 | issue→需求/缺陷，PR 默认跳过 |
| （若无） | 优先级 | 建议补 P0-P3 或 高/中/低 |
| （若无） | 模块 | 按代码目录维度补（app/core、services…） |

## 4. 状态流转映射

飞书项目状态流（推荐五段式，覆盖研发全流程）：

```
待办 → 需求评审 → 开发中 → 测试中 → 已完成
```

| GitHub 状态 | 飞书状态 | 备注 |
| --- | --- | --- |
| `open` | 待办 | 导入默认 |
| （自定义字段/项目） | 进行中/开发中 | GitHub 原生无此状态，需人工补充 |
| `closed` | 已完成 | 按关闭动作映射 |

建议同时定义流转规则：需求类只能正向流转；缺陷可由「测试中」回退到「开发中」。

## 5. 标签体系映射

GitHub 常见标签规整到飞书标签（建议保留「模块 + 类型」两维）：

| GitHub label | 飞书标签 | 维度 |
| --- | --- | --- |
| `bug` | 缺陷 | 类型 |
| `enhancement` / `feature` | 增强/需求 | 类型 |
| `backend` / `frontend` / `test` | 后端/前端/测试 | 负责人域 |
| `docs` / `devops` / `ci` | 文档/运维/CI | 模块 |
| `p0`/`p1`/`p2` | 高/中/低 | 优先级 |
| 项目自定义（immich/ranking/vision…） | 按模块保留 | 模块 |

## 6. 成员与权限映射

飞书项目默认角色：项目管理员、成员、访客。

| 团队角色 | 飞书角色 | 权限建议 | 对应模块 |
| --- | --- | --- | --- |
| 后端开发 | 项目管理员/成员 | 编辑工作项、管理迭代 | app/core、app/services |
| 前端/客户端 | 成员 | 编辑消费端相关需求 | docs/消费端 |
| 测试/QA | 成员 | 编辑缺陷状态、验收流转 | tests |

> 导入时把 CSV 中的「负责人」列（后端开发/前端开发/测试工程师）替换为具体飞书成员姓名或邮箱，飞书会自动匹配账号。

## 7. 工作项清单

已生成 `migration/work_items.csv`（15 条，UTF-8-BOM，Excel 可直接打开）。含：

- **已完成（7 条）**：对应首版已交付功能（框架、Immich 客户端、二次排序、视觉抽象、错误码、测试、容器化）。
- **待办（8 条）**：对应预留扩展点（华为云真实接入、排序策略化、契约测试、Redis/限流、监控、SDK、集成测试、CI/CD）。

如需调整，编辑 `migration/generate_work_items.py` 后重跑：

```bash
python migration/generate_work_items.py
```

## 8. 分步迁移 SOP

1. **确定路径**：有真实 remote→走路径 A 导出；否则直接用 `work_items.csv`。
2. **创建飞书项目**：新建项目并设工作项类型（需求/任务/缺陷）。
3. **配字段**：按 §3 建自定义字段（优先级、模块、里程碑、标签）。
4. **配状态流**：按 §4 建五段状态，设流转规则。
5. **配标签**：按 §5 建标签字典。
6. **导入**：飞书项目 → 更多 → 导入数据（Excel/CSV），按列名匹配字段。
7. **映射成员**：将负责人替换为真实成员，设角色权限（§6）。
8. **配自动化**：见下节。
9. **校验**：抽查已完成/待办数量、负责人、状态是否符合预期。

## 9. 自动化规则建议

- 工作项流转到「已完成」时：自动记录完成时间、通知负责人。
- 缺陷类型创建时：自动指派给对应模块负责人。
- 任务进入「开发中」时：提醒关联源码分支/提交。
- 每日未更新提醒：超过 N 天无进展的进行中任务自动 @ 负责人。

## 10. 待你补充的信息（用于精确执行路径 A / 成员映射）

1. 真实 GitHub/GitLab 仓库地址（owner/repo 或 project id）。
2. 可用的 Token（或确认由你本地自行导出）。
3. 团队成员的真实姓名/邮箱（用于替换「负责人」占位与权限配置）。
4. 是否需要在飞书项目里额外增加「产品/需求」「运维/DevOps」角色。

> 说明：本迁移在本机执行，无法直连飞书项目后台；以上产物（CSV + 映射文档 + 导出脚本）即为你可在飞书后台完成导入的全部素材。