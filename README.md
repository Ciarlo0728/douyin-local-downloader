# Douyin Local Downloader

一个在 Windows 本机运行的抖音作品扫描与按需下载工具。

输入作者主页后，可以浏览封面、标题、日期和时长，筛选并勾选需要的视频；也可以直接粘贴单条视频链接下载。登录状态、扫描历史和下载记录只保存在本机。

![界面预览](./界面预览.png)

## 功能

- 扫描作者主页并分批继续加载
- 单条视频链接和分享文字解析
- 标题关键词、已下载状态筛选
- 勾选下载、并行任务、取消和失败重试
- 重复文件检查与自动跳过
- SQLite 本地历史记录
- 独立 Edge 用户目录，不读取日常浏览器资料
- 默认下载到 `E:\BaiduNetdiskDownload`，可在界面中修改

图文作品目前只显示在列表中，暂不支持下载。直播、收藏夹和定时监控不在当前版本范围内。

## Windows 安装

需要：

- Windows 10/11
- Python 3.12
- Microsoft Edge

安装与启动：

1. 下载或克隆本仓库。
2. 双击 `setup.cmd` 安装 Python 依赖。
3. 双击 `start.cmd` 启动，本地页面会自动打开。
4. 点击“登录抖音”，在工具打开的独立 Edge 窗口中登录。

服务只监听 `127.0.0.1:18765`，不会部署到公网。用完可点击页面左下方“退出工具”，或双击 `stop.cmd`。

更完整的操作和状态说明见 [使用说明](./使用说明.md)。真实素材验证结果见 [验证记录](./验证记录.md)。

## 开发与测试

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.venv\Scripts\python.exe -m pytest -q tests
.venv\Scripts\python.exe -m ruff check .
```

## 数据与隐私

以下目录不会提交到 Git：

- `.venv/`：Python 虚拟环境
- `data/`：登录状态、Cookie、封面缓存、SQLite 历史和日志
- `downloads/`：下载的视频

不要将运行后的 `data/browser/` 分享给他人，其中包含你的抖音登录状态。

## 上游与许可证

媒体解析和下载能力复用 [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader)，固定于提交 `1f540317aa09338e1c33abdd6099c53c1918caf5`。上游源码位于 `vendor/douyin-downloader/`，保留原 MIT 许可证和版权声明。

本项目新增的本地网页、Edge 页面扫描适配、任务管理和历史记录代码使用 MIT 许可证，见 [LICENSE](./LICENSE)。

本工具仅用于下载你有权访问和保存的内容。使用者应遵守抖音服务条款、著作权规则及所在地法律，不要用于绕过访问控制或未经授权传播内容。

