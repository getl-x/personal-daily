# 个人信息日报

每天北京时间 08:30 自动读取免费 RSS/Atom 信息源，去重、分类并生成适合手机阅读的静态日报，然后部署到 GitHub Pages。

## 当前信息源

- 科技：GitHub Blog、Ars Technica、TechCrunch、MIT Technology Review、The Verge
- GitHub 热门项目：Visual Studio Code、Ollama、Rust、uv、PyTorch、Kubernetes、Deno、Godot Engine
- 游戏：PC Gamer、Eurogamer、PlayStation Blog、Xbox Wire
- 时政热点：德国之声中文、联合国新闻中文、Al Jazeera、The Guardian World、BBC World

共 22 个信息源。所有源都使用公开 RSS/Atom，不需要 API Key。

## 工作方式

1. GitHub Actions 每天定时运行 `generate.py`。
2. 脚本读取最近 48 小时的内容，并根据链接去重。
3. 当日最多展示 80 条，每个源最多 5 条。
4. 历史条目和网页归档保留 180 天。
5. 生成内容保存在 `site/` 并部署到 GitHub Pages。

也可以在仓库的 **Actions → Personal Daily → Run workflow** 中手动运行。

## 修改信息源

编辑 `config.json` 中的 `feeds`：

```json
{
  "name": "信息源名称",
  "category": "分类名称",
  "url": "https://example.com/feed.xml"
}
```

GitHub 项目的发布动态通常可以使用：

```text
https://github.com/OWNER/REPOSITORY/releases.atom
```

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python generate.py
```

生成结果位于 `site/index.html`。

## 隐私与费用

- 仓库和 Pages 页面是公开的，不要提交私人订阅地址、Token 或 Webhook。
- 当前版本不调用大模型 API，因此没有 AI API 费用。
- Workflow 使用标准 GitHub 托管 Runner，不使用大型 Runner。
