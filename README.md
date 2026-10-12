# ScienceDirect 论文篇名与摘要采集

一个独立的面试演示项目：选择 ScienceDirect 上的一篇真实论文，提取**篇名和完整摘要**，导出 CSV、JSON 和可访问的静态展示页。按本次要求，成果为 **1 篇**；程序可接受多个文章 URL，但本项目没有声称抓取整本期刊。

## 已完成的真实样本

- 期刊：**Procedia Computer Science**，第 246 卷，2024 年，5458–5468 页
- 论文：**Generative AI and Large Language Models - Benefits, Drawbacks, Future and Recommendations**
- 作者：Anne Håkansson、Gloria Phillips-Wren
- 原文：[ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1877050924027492)
- DOI：[10.1016/j.procs.2024.09.689](https://doi.org/10.1016/j.procs.2024.09.689)
- 原文许可：[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)，保留原文署名及链接，仅用于非商业演示
- 真实页面采集时间：**2026-10-12 13:35:17（北京时间）**

摘要是页面上显示的完整英文原文，没有用 AI 改写、补全或生成。作者名来自可见页面；结果文件的篇名、摘要及期刊标识均由 `scraper.py` 从保存的页面片段解析。

## 快速查看

**在线成果：[论文篇名与摘要展示页](https://wangxinwu555.github.io/sciencedirect-journal-scraper/)**

**独立仓库：[wangxinwu555/sciencedirect-journal-scraper](https://github.com/wangxinwu555/sciencedirect-journal-scraper)**

直接打开 `docs/index.html` 即可查看，支持本地文件打开，无需前端依赖或构建。也可启动静态服务：

```powershell
python -m http.server 8765 --directory docs --bind 127.0.0.1
```

打开 <http://127.0.0.1:8765>。CSV 可用 Excel 打开（UTF-8 BOM），JSON 包含结构化字段、来源和采集记录。

## 复现已有真实成果（离线即可）

Python **3.10 或以上**，只使用标准库，无需 API key、tokens、pip 或第三方依赖。

```powershell
python scraper.py --html raw/article.html --source-url https://www.sciencedirect.com/science/article/pii/S1877050924027492 --provenance raw/capture.json --out docs
python -m unittest discover -s tests -v
```

`raw/article.html` 是正常加载的 ScienceDirect 网页中，标题、摘要、期刊链接、citation 元数据和许可链接的 **DOM 片段**，并非伪造的 HTTP 响应，也不是完整网页存档。`raw/capture.json` 记录采集方式、源地址和时间。解析时计算片段的 SHA-256，便于核查。

## 在线请求模式

```powershell
python scraper.py --url https://www.sciencedirect.com/science/article/pii/S1877050924027492 --out outputs/live
```

多个 `--url` 会去重，并按顺序访问，默认每篇之间间隔 2 秒。每条输入必须是 HTTPS ScienceDirect 文章地址。

**本次环境的普通 HTTP 请求被 ScienceDirect 返回 403，因此最终成果使用正常浏览器加载页面 → 保存 DOM 片段 → Python 解析的路径。HTTP 直连模式尚未在本环境成功，不应把此项目描述为“已突破反爬”或“全自动在线批量抓取”。** 程序遇到 401/403/429、缺少完整摘要或页面标识不匹配时会返回非零退出码，不会生成虚假成功记录。

如果另一篇文章在浏览器中可正常访问，而程序被拦截，可用浏览器“另存为”保存 HTML，然后运行：

```powershell
python scraper.py --html path/to/saved-article.html --source-url https://www.sciencedirect.com/science/article/pii/ARTICLE_PII --out outputs/another
```

这一模式的数据来源标记为 `saved-html`；没有采集记录时不编造采集时间。网站登录页、验证码、无完整摘要的截断页面应先在正常浏览器里处理，不绕过访问限制。

## 输出与实现

| 文件 | 用途 |
| --- | --- |
| `scraper.py` | HTTP 获取、HTML 结构解析、验证、去重和导出 |
| `raw/article.html` | 本次真实浏览器页面的源 DOM 片段 |
| `raw/capture.json` | 采集来源和实际 HTTP 失败记录 |
| `docs/articles.csv` | 篇名、摘要、期刊、DOI、PII、时间与来源等字段 |
| `docs/articles.json` | 同一份数据的结构化 JSON |
| `docs/index.html` | 响应式结果页、关键词筛选、下载、来源详情 |
| `docs/evidence/` | 随展示页附带的来源证据 |
| `templates/index.html` | 展示页模板 |
| `tests/test_scraper.py` | 完整摘要边界、错误页面拒绝、来源校验、导出一致性和 HTML/CSV 转义检查 |

使用 `HTMLParser` 构建简易节点树，优先定位正文标题 `screen-reader-main-title` 和摘要 `abs0001`，剔除摘要内标题，保留内嵌文本。不会用全文匹配把参考文献或推荐论文混入摘要；也不会把通常被截断的 description 元数据当作完整摘要。若网站改版，解析器明确失败，需要调整选择器。

页面无后端、追踪器或外部 CDN；抓取在本地运行，网页只展示结果。已有成果可离线复现；网络采集是否成功取决于网站访问状态。

## 提交面试

仓库链接可用于查看代码、CSV 与 JSON。已启用 GitHub Pages，来源为 `main` 分支下的 `/docs` 目录；上面的在线成果链接已经验证可正常访问。更新展示成果后提交并推送到 `main`，GitHub Pages 会重新发布。

建议发送说明：

> 您好，我选取了 Procedia Computer Science 的一篇论文，完成了篇名和完整英文摘要的采集，附原文链接、CSV/JSON 导出和可复现代码。本次采用浏览器页面采集后解析的方式，普通 HTTP 请求被网站返回 403；实现与采集记录已在仓库说明。
>
> 成果展示：https://wangxinwu555.github.io/sciencedirect-journal-scraper/
>
> 代码仓库：https://github.com/wangxinwu555/sciencedirect-journal-scraper

项目代码采用 MIT License；论文原文、摘要与来源片段采用其原有 CC BY-NC 4.0 许可，**不受项目代码 MIT 许可覆盖**。
