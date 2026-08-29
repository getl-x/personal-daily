# 今日知览

每天北京时间 08:30 自动读取免费的公开信息源，去重、分类并生成适合手机和电脑阅读的静态日报，然后部署到 GitHub Pages。

首页默认展示“今日精选”，通过下拉框可以快速切换频道，未选中的内容会真正隐藏，避免在长页面中反复滚动。页面支持明暗主题、桌面端双列卡片和手机端单列布局，并会在浏览器中记住所选频道与主题。

## 内容频道

- 今日精选：从当天各频道轮流挑选最多 10 条较重要的内容，不调用 AI，也不产生 API 费用
- 科技：IT之家、少数派、爱范儿、极客公园、小众软件
- 财经商业：中国新闻网财经、人民网财经、第一财经、每日经济新闻、钛媒体
- 科学探索：中国科学院科研进展、科学网、人民网科技、中国新闻网健康、人民网健康
- 游戏：机核、游民星空、3DM
- 国内要闻：中国新闻网即时、人民网时政、新华网时政、央广网国内、中国新闻网社会

共配置 23 个中文大陆信息源。优先使用 RSS/Atom；对没有稳定订阅源的网站，只读取公开列表页中的标题、摘要、时间和原文链接，不抓取全文。

## 工作方式

1. GitHub Actions 每天定时运行 `generate.py`。
2. 脚本读取最近 48 小时的内容，统一时间格式并根据链接和标题去重。
3. 每个普通频道最多展示 15 条，今日精选最多展示 10 条。
4. 历史数据和每日网页归档保留 180 天。
5. 生成结果保存在 `site/`，随后自动部署到 GitHub Pages。
6. 某个来源暂时不可用时，脚本会记录错误并继续处理其他来源，不会让整份日报中断。

也可以在仓库的 **Actions → Personal Daily → Run workflow** 中手动运行一次。

## 修改信息源

默认版本的信息源配置位于 `config.cn.json`。RSS/Atom 来源的基本格式如下：

```json
{
  "name": "信息源名称",
  "category": "分类名称",
  "url": "https://example.com/feed.xml"
}
```

HTML 列表来源还可以配置文章、标题、链接、摘要和时间等 CSS 选择器。具体写法可以参考 `config.cn.json` 中已有的同类来源。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python generate.py
```

生成后的首页位于 `site/index.html`，历史归档位于 `site/archive/`。

## 运行环境与费用

- Workflow 的构建和部署任务都使用 GitHub 托管的 `ubuntu-latest` Runner。
- 当前版本不调用大模型 API，也不需要任何付费数据接口。
- 仓库和 Pages 页面是公开的，请不要提交私人订阅地址、Token、Cookie 或 Webhook。
- GitHub Actions 和 Pages 的具体免费额度取决于仓库可见性与 GitHub 当前政策；公开仓库使用标准托管 Runner 通常不会按私有仓库的 Actions 分钟额度计费。
