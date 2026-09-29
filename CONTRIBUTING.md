# 一起写这个仓库

## 记笔记

- 文档用中文。
- 一篇学习记录一个 Markdown 文件，放在 `journal/`。
- 文件名用日期加短标题，例如 `journal/2026-09-29-agent-loop.md`。
- 从 [journal/_template.md](journal/_template.md) 复制结构。用不上的小节可以删。
- 不要把密钥、令牌或 cookie 写进笔记。命令输出里如果出现密钥，打码后再贴。

## 加实验

- 每个实验一个目录：`labs/NN-短名/`，例如 `labs/01-minimal-loop/`。
- 目录里放一份能单独看懂的 `README.md`：这个实验在验证什么、怎么跑、跑完看到了什么。
- 仓库根目录先不放统一的依赖文件。等到第一个实验真正需要时，再把依赖写清楚。

## 提交

- 共同作者用 Pull Request 把改动合进 `main`，避免两个人同时改同一篇笔记时互相覆盖。
- 只改自己的笔记，或改动很小并且已经说好时，可以直接推到 `main`。
- 提交说明写清楚改了哪一篇笔记，或哪一个实验。

## 邀请共同作者

建仓时好友的 GitHub 登录名还没确认，所以还没有邀请任何人。确认之后可以随时补上，不需要重建仓库。

被邀请之前，别人已经可以阅读这个公开仓库、Fork，以及提 Pull Request。邀请之后，对方才有直接向这个仓库推送的权限。

网页上的做法：打开仓库的 **Settings → Collaborators → Add people**，权限选 **Write**。

命令行（把 `OWNER` 换成仓库主人的登录名，把 `USERNAME` 换成好友的登录名）：

```bash
gh api --method PUT repos/OWNER/ai-agent-learning/collaborators/USERNAME -f permission=push
```

`permission=push` 就是网页上的 Write。
