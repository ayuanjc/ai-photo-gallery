# 审查demo与readme部署件 (@general subagent)

- 会话 ID：ses_eabe2c833ffe5VzPNPz85GTD9y
- 工作目录：a01
- 消息数：8　代码改动：0 次　轮次范围：第 1-1 轮

## 第 1 轮 · 2026-10-19 20:22:43

**用户：**

```text
你是资深防御性编程代码审查专家。对 C:\Users\ayuan\IDEProjects\a01 项目的 demo/ 目录和 readme/ 目录做第五轮防御性编程审查。**只读审查，禁止修改任何文件。**

## 项目背景
「云影图鉴」：AI 图像语义检索微服务（比赛交付前最后一轮审查）。技术栈：Python 3.11+ / FastAPI / Immich(自托管相册)。demo/ 是比赛现场演示站的部署与前端件（部署到公网 106.52.116.164，Ubuntu + systemd + nginx）；readme/ 是交付文档与安装脚本（评委/用户会照着执行 install.sh 部署）。语言规则：最终报告用简体中文。

## 审查范围（用 ls/glob 先列目录确认实际文件，逐文件全读）
- demo/ 下全部文件（serve_demo.py、web/index.html、deploy.sh、nginx.demo.conf、yunying-demo.service 等；**跳过 deploy_monitor.sh——那是用户自制未提交文件**）
- readme/ 下全部文件（install.sh、01~04 文档等）

## 已知已修复问题（前四轮，不要重复报告）
- F0：install.sh systemd unit 已改 uvicorn 启动 + 正确环境变量名 DEMO_BASE/DEMO_CLIP_DIR
- F8：demo/web/index.html 已加 esc() 转义 query/translated/scene/id/thumb
- admin.py settings 写 .env 已需 ADMIN_API_KEY
- 各路由鉴权（ratings 路由缺鉴权问题已在 app/ 审查中单独发现，本轮不重复报）

## 审查维度（A-J）
- A 输入验证：URL 参数、用户输入进 DOM/shell/env 的路径
- B 注入防护：shell 命令拼接（deploy/install 脚本中变量未加引号、curl|bash、eval）、HTML 注入（XSS，除已修复的 esc() 五处外还有没有漏网字段，如 onerror/onload/innerHTML/template 拼接点）、CRLF
- C 认证授权：演示站端点暴露、管理接口
- E 敏感数据：脚本/配置中硬编码 IP、key、token、密码；错误页泄漏
- F 资源管理：无界缓存、内存泄漏
- G 错误处理：脚本 set -e 缺失、错误后继续执行、fail-open
- H 日志：日志打印敏感信息
- I 并发：事件循环阻塞
- J 配置部署：nginx 配置缺陷（缺安全头、目录列表、错误页泄漏版本）、systemd 加固缺失（无 ProtectSystem/NoNewPrivileges 等，按 Medium 以下酌情）、CORS、监听地址（0.0.0.0 vs 127.0.0.1）、默认口令
- 文档一致性：install.sh 引用的文件/端口/路径与实际代码是否一致（前轮 F0 类问题的残留）

## 严重程度定义
- Blocker：部署即失败/演示现场崩溃/公网可入侵
- High：特定条件下安全问题或部署故障
- Medium：加固缺失/异常行为/文档误导
- Low：不报

## 硬性约束
- 每条发现必须有 file:line 精确定位和代码原文证据
- 置信度 <7 不报
- 不虚构代码；不确定先读文件验证
- 注意：demo 演示站是"演示环境"，威胁模型为公网可访问但数据无敏感性——但仍按公网标准审（评委可能现场扫描）

## 输出格式（最终回复，简体中文）
1. 实际读过的文件清单
2. 发现列表（按严重程度排序）：R5D-x 编号、严重程度、置信度、位置、标题、代码证据、攻击/故障场景、维度
3. 检查后未发现问题的维度
4. 候选问题过滤说明（置信度<7 的）
```

**码道：**

我先列出 demo/ 和 readme/ 目录的实际文件清单，确认审查范围。

**码道：**

共 10 个待审文件（跳过 deploy_monitor.sh）。注意到 demo/ 下还有一个 52KB 的 query_translate.py，属于"全部文件"范围，一并审查。开始逐文件读取。
