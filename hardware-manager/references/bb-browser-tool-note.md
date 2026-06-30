# bb-browser 工具说明

## 性质

bb-browser 是 **npm 安装的 CLI 程序**（浏览器自动化工具），不是 Hermes 技能。

- 安装路径：`/opt/homebrew/bin/bb-browser`（brew 安装）
- 用户数据：`~/bb-browser` 和 `~/.bb-browser/`
- GitHub：`github.com/epiral/bb-browser`

## bb-browser 能做什么

通过 Chrome DevTools Protocol (CDP) 控制真实浏览器，让 AI Agent 使用你的登录态操作任意网页。

核心场景：
- B站/小红书/微博/知乎等国内平台内容抓取（用你的 Cookie）
- 需要登录态的网页操作
- 35+ 平台已有 adapter（site 系统）

## bb-browser Skill

bb-browser GitHub 上有现成的 `skills/bb-browser/SKILL.md`（Hermes skill 格式），可以安装到 `~/.hermes/skills/bb-browser/`。

安装方式：从 GitHub 下载 SKILL.md 和 references/ 目录内容。

## 触发条件

当用户提到 bb-browser 时：
1. 说明它是程序不是技能
2. 如需封装为技能，告知可以从 GitHub 安装
