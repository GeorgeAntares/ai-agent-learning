# AI Agent 学习记录

公开笔记，用来记录学习 AI Agent 开发的过程，也方便以后和好友一起改。

本地文件夹可以叫别的名字。GitHub 仓库名是 `ai-agent-learning`。

## 现在到哪了

仓库刚建好，只有笔记骨架。还没有可运行的实验，也还没有邀请共同作者。好友的 GitHub 登录名确认之后再邀请，不影响这个仓库的使用。

## 目录

| 路径 | 内容 |
| --- | --- |
| [roadmap/README.md](roadmap/README.md) | 学习路线：每阶段学什么、怎样算完成 |
| [journal/](journal/README.md) | 一篇一篇的学习记录。模板在 `journal/_template.md` |
| [labs/](labs/README.md) | 以后每个实验一个子目录。这一步是空的 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 怎么记笔记、怎么提 PR、怎么邀请共同作者 |

## 路线概览

1. 概念：代理循环、工具、记忆、规划、人在回路中
2. 最小循环：不用重框架，走通一次「思考 → 调工具 → 看结果」
3. 工具调用：用一个轻量 SDK 把工具接上
4. 多代理协作：角色分工（计划用 CrewAI）
5. 有状态的长流程：检查点、分支、人工审批（计划用 LangGraph）
6. 评测与追踪：怎么判断做对了，以及密钥和权限

每一阶段的完成标准写在 [roadmap/README.md](roadmap/README.md)。

## 密钥

真实的 API Key 只放在本机的 `.env` 里。`.env` 已被 `.gitignore` 忽略。

需要新变量时，把变量名写进 `.env.example`，不要写值。

## 许可

笔记和以后的示例代码使用 [MIT](LICENSE) 许可。
