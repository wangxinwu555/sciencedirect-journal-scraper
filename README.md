# ScienceDirect 期刊篇名与摘要批量采集

从 ScienceDirect 官网采集 **Procedia Computer Science 2024 年第 231–251 卷**的论文篇名与原文摘要，按 PII 去重，导出结构化数据及来源证据。

**本次展示成果：3,269 篇篇名与英文摘要，涵盖 21 卷。** 摘要直接读取官网 Abstract 容器，未使用 AI 改写或补全。

- [在线查看采集成果](https://wangxinwu555.github.io/sciencedirect-journal-scraper/batch-2024/)
- [下载 CSV](https://wangxinwu555.github.io/sciencedirect-journal-scraper/batch-2024/articles.csv)
- [下载 JSON](https://wangxinwu555.github.io/sciencedirect-journal-scraper/batch-2024/articles.json)
- [下载来源证据](https://wangxinwu555.github.io/sciencedirect-journal-scraper/batch-2024/source-evidence.zip)

## 执行采集

需要 Python 3.10+ 与已安装的 Chrome。浏览器由代码启动，使用项目独立配置；不需要 API key。

```powershell
python -m pip install -r requirements-website.txt
python website_collect_cdp.py --year 2024 --out website/2024-cdp --show-all-previews
```

相同命令再次执行会从 SQLite 断点继续，并跳过已保存的文章。按官网年份面板确定卷期范围，遵循真实 Next page 控件翻页，按文章 PII 去重。读取卷期中的 Article preview，必要时读取官方文章详情页；成功记录保留原始 DOM、时间、来源 URL 与 SHA-256。

新建空白浏览器配置的两轮实测各新增 5 篇，共 10 篇，第二轮跳过首轮结果；篇名及摘要样本与文章详情页核对一致。

## 生成展示成果

发布生成器从实际采集文件选择有摘要的记录，并逐篇校验来源哈希，生成展示页、CSV、JSON 与证据 ZIP。

```powershell
python publish_results.py --source website/2024-cdp --out docs/batch-2024
python verify_published.py --path docs/batch-2024
python -m http.server 8765 --directory docs --bind 127.0.0.1
```

本地访问 <http://127.0.0.1:8765/batch-2024/>。展示页支持卷号浏览、篇名/作者/DOI 快速查找以及英文摘要展开。

## 代码与数据

| 文件 | 用途 |
|---|---|
| `website_collect_cdp.py` | Chrome CDP 采集、年度发现、分页、摘要读取及断点续采 |
| `website_collect.py` | SQLite 任务状态、导出和浏览器对照实现 |
| `scraper.py` | 官网标题、摘要及 citation 元数据解析 |
| `publish_results.py` | 构建当前采集成果及证据包 |
| `verify_published.py` | 离线核对公开数据和来源证据 |
| `docs/batch-2024/` | 本次可分享的展示页与数据 |
| `tests/` | 内容边界、来源校验、去重和完成状态检查 |

```powershell
python -m unittest discover -s tests -v
```

抓取数据来自 ScienceDirect 官网，不依赖 Crossref 或 OpenAlex 补全摘要。浏览器配置、运行数据库和诊断资料保留在本机，加入 Git 忽略。原始证据可按 `raw/PII.html` 与 JSON 的 `source_sha256` 核对。

项目代码采用 MIT License。论文原文、摘要及来源片段保留原有作者、出版社版权及许可，具体以原文页面标注为准，不受项目代码许可证覆盖。
