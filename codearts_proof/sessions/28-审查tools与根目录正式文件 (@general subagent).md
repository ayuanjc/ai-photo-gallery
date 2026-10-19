# 审查tools与根目录正式文件 (@general subagent)

- 会话 ID：ses_eabdb29ebffeh0O7bk5vxv3Hr6
- 工作目录：a01
- 消息数：16　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-19 20:31:02

**用户：**

```text
你是资深防御性编程代码审查专家。对 C:\Users\ayuan\IDEProjects\a01 项目中 git 跟踪的 tools/ 脚本与根目录杂项文件做第五轮防御性编程审查。**只读审查，禁止修改任何文件。**

## 项目背景
「云影图鉴」：AI 图像语义检索微服务（比赛交付前最后一轮审查）。技术栈：Python 3.11+ / FastAPI / Immich。这些 tools/ 脚本部分是开发调试探针（评委可能会浏览源码），部分是交付配套工具（pack_submission.py 打包提交物、dump_codearts_proof.py 生成证据包）。语言规则：最终报告用简体中文。

## 审查范围（逐文件全读，已用 git ls-files 确认跟踪清单）
tools/ 下 13 个文件：
- build_demo_assets.py、build_demo_assets2.py、check_page.py、contract_probe.py、demo_cache_check.py、demo_ext_check.py、demo_probe.py、dump_codearts_proof.py、inspect_onnx_ext.py、pack_submission.py、probe_smart.py
（注意：tools/ 下 `_*` 前缀文件、dsh-server-monitor/、monitor-tunnel.ps1、toggle-server-monitor.ps1、stage_project.py 均为用户自制未跟踪文件，跳过不审）

根目录跟踪文件（重点审这几个，其余配置文件粗查即可）：
- brute_test.py（混沌测试脚本）、verify_integration.py、Dockerfile、docker-compose.yml、docker-compose.override.yml、nginx.conf、.env.example、start_server.cmd
（README.md、USAGE.md、LICENSE、pyproject.toml、requirements*.txt、submit_log.md、.gitignore、.dockerignore 只查敏感信息泄漏，不审内容质量）

## 已知已修复问题（前四轮，不要重复报告）
- ratings 路由无鉴权（app/ 审查已发现）
- install.sh/deploy.sh/demo 部署件问题（demo/readme 审查已发现）
- SSRF/限流/写回闸门等 app/ 问题均已修复

## 审查维度
- B 注入：subprocess shell=True、os.system、eval、路径拼接遍历
- E 敏感数据：硬编码 IP/key/token/密码/内网地址泄漏到交付物；.env.example 中的示范值是否安全
- F 资源：无界循环/内存
- G 错误处理：except 吞异常、fail-open 导致误报通过
- I 并发：线程安全问题
- J 配置部署：Dockerfile 以 root 运行、docker-compose 危险配置（privileged、密码弱默认）、nginx.conf 缺陷、.env.example 危险默认值（如默认 key=changeme 且生产不强制改）
- 交付卫生：探针脚本中的调试残留、TODO/FIXME、会被评委看到的尴尬内容、verify_integration.py 对不上的断言

## 严重程度定义
- Blocker：交付必失败/公网可入侵
- High：特定条件下安全问题或交付故障
- Medium：加固缺失/交付卫生问题/文档误导
- Low：不报

## 硬性约束
- 每条发现 file:line + 代码原文证据；置信度 <7 不报；不虚构；不确定先读文件验证
- nginx.conf 是 app/ 主服务的前置（转发 /api/v1/ 到 search_api），上一审查已覆盖其转发部分，本轮只看整体配置卫生（安全头/版本号/限流），不要重复报 ratings 类问题

## 输出格式（最终回复，简体中文）
1. 实际读过的文件清单
2. 发现列表（按严重程度排序）：R5T-x 编号、严重程度、置信度、位置、标题、代码证据、故障/攻击场景、维度
3. 检查后未发现问题的维度
4. 候选问题过滤说明
```

**码道：**

跟踪清单确认：tools/ 下 11 个跟踪文件（用户提及 13 个，实际 git 跟踪 11 个，`_*` 与 stage_project.py 未跟踪已跳过）。开始逐文件全读。

**码道：**

小文件已读完。已发现多个探针脚本硬编码公网 IP `106.52.116.164`。继续读大文件与根目录文件。
