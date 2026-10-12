# 官网批量采集运行说明

本版采集 Procedia Computer Science 指定年份的篇名与原文摘要，读取 ScienceDirect 年份目录、卷期分页与文章对应的 Abstract 容器。

```powershell
python -m pip install -r requirements-website.txt
python website_collect_cdp.py --year 2024 --out website/2024-cdp --show-all-previews
```

相同命令再次执行会从已有 SQLite 记录继续，跳过已采集文章。默认 Chrome 使用输出目录内的独立浏览器配置；Edge 可用 `--channel msedge`，自定义安装路径用 `--browser`。需要复用其他项目独立配置时使用 `--profile`，不要使用日常浏览器配置。

## 小规模执行与离线导出

```powershell
python website_collect_cdp.py --year 2024 --out website/check --max-volumes 1 --max-articles 5 --show-all-previews
python website_collect_cdp.py --year 2024 --out website/2024-cdp --export-only
```

`--show-all-previews` 操作官网的全部摘要开关；没有该控件时按文章按钮展开。逐页读取 Next page，校验 PII 与官网链接，保存每篇对应的真实原始 DOM。年份依据卷期目录，不能以 DOI 路径年份替代期刊年份。

浏览器保持正常安全设置，绑定本机调试连接。页面访问异常或内容校验失败时保存任务状态和本地诊断；下次执行继续处理。空列表和错误页面不会冒充论文数据。

## 发布采集成果

```powershell
python publish_results.py --source website/2024-cdp --out docs/batch-2024
python verify_published.py --path docs/batch-2024
```

发布文件包括篇名、原文摘要、官网显示作者、DOI、PII、卷号、页码、原文链接、采集时间与来源 SHA-256。每个来源片段保存在证据 ZIP 中，可离线重新解析核对。展示结果仅包括已采集且具备原文摘要的记录。

运行数据库、浏览器会话与诊断截图只存本机。公开成果不包含账号、浏览器配置、机器路径或诊断资料。
