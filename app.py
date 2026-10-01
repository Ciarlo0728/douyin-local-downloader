"""Local Douyin library: browser discovery plus the pinned upstream downloader."""

import asyncio
import os
import re
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DOUYIN_LOCAL_DATA", str(ROOT / "data")))
DATA.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "vendor" / "douyin-downloader"))

import logging

from auth import CookieManager
from config import ConfigLoader
from control import RateLimiter, RetryHandler
from core import DouyinAPIClient, URLParser
from core.video_downloader import VideoDownloader
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from storage import FileManager
from utils.logger import set_console_log_level

from browser_gateway import BrowserGateway
from store import Store

set_console_log_level(logging.CRITICAL)

TOKEN = secrets.token_urlsafe(32)
PORT = 18765
store = Store(DATA / "library.sqlite3")
browser = BrowserGateway(DATA, store.save_items)
tasks = {}
server_instance = None
slots = asyncio.Semaphore(2)
download_lock = {}
scan_lock = asyncio.Lock()
auth = {"busy": False, "message": "点击登录抖音，使用独立浏览器保存本机登录状态"}
settings = store.get("settings", "main", {"output": "E:\\BaiduNetdiskDownload"})
Path(settings["output"]).mkdir(parents=True, exist_ok=True)


def save(kind, value):
    value["updated"] = time.time()
    store.put(kind, value["id"], value)


def public_error(exc):
    if isinstance(exc, asyncio.TimeoutError):
        return "操作超时，请检查网络、登录状态后重试"
    if "TargetClosed" in type(exc).__name__ or "closed" in str(exc).lower():
        return "抖音浏览器窗口已关闭，请重新打开登录窗口后重试"
    if isinstance(exc, RuntimeError):
        return re.sub(r"https?://\S+", "[请求地址]", str(exc))[:220]
    return "操作未完成（" + type(exc).__name__ + "），请检查登录和网络后重试"


def spawn(key, coroutine):
    task = asyncio.create_task(coroutine)
    tasks[key] = task
    task.add_done_callback(lambda finished: tasks.pop(key, None) if tasks.get(key) is finished else None)


def clean_link(value):
    match = re.search(r"https?://[^\s<>\[\]（）]+", value)
    if not match:
        raise ValueError("请粘贴抖音主页或视频链接，也可以粘贴完整分享文字")
    url = match.group().rstrip(').,，。;；"')
    parsed = urlsplit(url)
    if parsed.hostname not in {
        "www.douyin.com",
        "douyin.com",
        "v.douyin.com",
        "v.iesdouyin.com",
        "www.iesdouyin.com",
    }:
        raise ValueError("这里只接受抖音主页、视频及官方分享短链接")
    if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("链接格式不正确")
    return url


async def parse_link(url):
    if urlsplit(url).hostname in {"v.douyin.com", "v.iesdouyin.com"}:
        url = await browser.resolve(url)
        url = clean_link(url)
    parsed = URLParser.parse(url)
    if not parsed or parsed["type"] not in {"user", "video"}:
        raise RuntimeError("第一版支持作者主页和单条视频，请使用对应链接")
    key = "sec_uid" if parsed["type"] == "user" else "aweme_id"
    if not parsed.get(key):
        raise RuntimeError("未识别到作者或视频 ID")
    return parsed


async def scan_worker(scan):
    try:
        async with scan_lock:
            scan.update(status="running", message="正在打开抖音并读取作品…")
            save("scan", scan)
            parsed = await parse_link(scan["url"])
            scan["type"] = parsed["type"]
            if parsed["type"] == "video":
                item = await browser.detail(parsed["aweme_id"])
                store.save_items([item])
                scan.update(
                    ids=[str(item["aweme_id"])], status="complete", message="已解析单条视频，可选择下载"
                )
            else:
                scan["uid"] = parsed["sec_uid"]

                def progress(ids, pages):
                    scan.update(ids=ids, pages=pages, message=f"已发现 {len(ids)} 条作品，正在继续读取…")
                    save("scan", scan)

                state, message, ids = await browser.scan(
                    scan["uid"], scan["target"], scan.get("ids", []), progress
                )
                scan.update(status=state, message=message, ids=ids)
            save("scan", scan)
    except asyncio.CancelledError:
        scan.update(status="stopped", message="已停止扫描，保留已发现作品")
        save("scan", scan)
        raise
    except Exception as exc:
        scan.update(status="restricted", message=public_error(exc))
        save("scan", scan)


class Reporter:
    def __init__(self, job):
        self.job = job

    def update_step(self, step, detail=""):
        self.job["message"] = detail or step
        save("job", self.job)

    def set_item_total(self, total, detail=""):
        pass

    def advance_item(self, status, detail=""):
        pass

    def on_item_progress(self, aweme_id, bytes_read, bytes_total):
        self.job.update(bytes_read=bytes_read, bytes_total=bytes_total, message="正在保存视频文件")
        save("job", self.job)

    def on_output_dir(self, path):
        self.job["folder"] = path
        save("job", self.job)


def find_downloaded(output, item_id):
    record = store.get("download", item_id, {})
    paths = record.get(str(Path(output).resolve()), [])
    return [p for p in paths if Path(p).is_file() and Path(p).stat().st_size > 0]


async def download_worker(job):
    try:
        async with slots:
            lock = download_lock.setdefault((job["output"], job["item_id"]), asyncio.Lock())
            async with lock:
                job.update(status="running", message="准备下载")
                save("job", job)
                paths = find_downloaded(job["output"], job["item_id"])
                if paths:
                    job.update(status="skipped", message="文件已存在，已跳过", files=paths)
                    save("job", job)
                    return
                item = store.raw(job["item_id"])
                if not item:
                    raise RuntimeError("作品信息不存在，请先扫描")
                if item.get("images"):
                    raise RuntimeError("这是一条图文作品，第一版只下载视频")
                cookies = await browser.cookies()
                # Cookies stay inside the browser profile. The upstream object is in-memory only.
                cm = CookieManager(str(DATA / "unused-cookie-file.json"))
                cm.cookies = cookies
                cfg = ConfigLoader()
                cfg.config.update(
                    path=job["output"],
                    thread=1,
                    folderstyle=False,
                    group_by_mode=False,
                    filename_template="{date}_{title}_{id}",
                    video_quality="highest",
                    json=False,
                    cover=False,
                    music=False,
                    avatar=False,
                    database=False,
                    proxy="",
                )
                cfg.config["transcript"]["enabled"] = False
                async with DouyinAPIClient(cookies) as client:
                    downloader = VideoDownloader(
                        cfg,
                        client,
                        FileManager(job["output"]),
                        cm,
                        rate_limiter=RateLimiter(max_per_second=1),
                        retry_handler=RetryHandler(max_retries=2),
                        progress_reporter=Reporter(job),
                    )
                    # Existing files from earlier sessions/installations also count.
                    existing = await asyncio.to_thread(
                        lambda: [
                            str(p)
                            for p in Path(job["output"]).rglob("*.mp4")
                            if job["item_id"] in p.name and p.stat().st_size > 0
                        ]
                    )
                    if existing:
                        paths, result_status = existing, "skipped"
                    else:
                        ok = await asyncio.wait_for(downloader._download_aweme(item), timeout=600)
                        if not ok:
                            # Refresh potentially expired media URLs once through the browser.
                            browser.details.pop(job["item_id"], None)
                            browser.items.pop(job["item_id"], None)
                            fresh = await browser.detail(job["item_id"])
                            store.save_items([fresh])
                            ok = await asyncio.wait_for(downloader._download_aweme(fresh), timeout=600)
                        if not ok:
                            raise RuntimeError("视频保存失败，可能是下载地址过期或网络受限；请重新扫描后重试")
                        paths = await asyncio.to_thread(
                            lambda: [
                                str(p)
                                for p in Path(job["output"]).rglob("*.mp4")
                                if job["item_id"] in p.name and p.stat().st_size > 0
                            ]
                        )
                        if not paths:
                            raise RuntimeError("未找到完整 MP4 文件，未标记为下载成功")
                        result_status = "success"
                record = store.get("download", job["item_id"], {})
                record[str(Path(job["output"]).resolve())] = paths
                store.put("download", job["item_id"], record)
                job.update(
                    status=result_status,
                    message="下载完成" if result_status == "success" else "文件已存在，已跳过",
                    files=paths,
                    bytes_read=sum(Path(p).stat().st_size for p in paths),
                    bytes_total=sum(Path(p).stat().st_size for p in paths),
                )
                save("job", job)
    except asyncio.CancelledError:
        job.update(status="cancelled", message="已取消，未完成文件不会计为成功")
        save("job", job)
        raise
    except Exception as exc:
        job.update(status="failed", message=public_error(exc))
        save("job", job)


@asynccontextmanager
async def lifespan(app):
    for kind in ("scan", "job"):
        for value in store.all(kind):
            if value.get("status") in {"queued", "running"}:
                value.update(
                    status="stopped" if kind == "scan" else "failed", message="上次运行中断，可手动继续或重试"
                )
                save(kind, value)
    yield
    running = list(tasks.values())
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    await browser.close()
    store.close()


app = FastAPI(title="抖音本地下载器", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if not secrets.compare_digest(request.headers.get("X-Local-Token", ""), TOKEN):
            return JSONResponse({"detail": "请从本机工具页面操作，刷新页面后重试"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def index():
    text = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    return text.replace("__LOCAL_TOKEN__", TOKEN)


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/api/health")
async def health():
    return {"app": "douyin-local", "status": "ok", "version": "0.1.0"}


@app.get("/api/state")
async def state():
    return {
        "settings": settings,
        "auth": {**auth, **await browser.status()},
        "scans": sorted(store.all("scan"), key=lambda x: x.get("updated", 0), reverse=True)[:30],
        "jobs": sorted(store.all("job"), key=lambda x: x.get("created", 0), reverse=True)[:200],
    }


@app.post("/api/login")
async def login():
    if not auth["busy"]:
        auth.update(busy=True, message="正在打开抖音登录窗口…")

        async def run():
            try:
                await browser.login()
                auth["message"] = "请在 Edge 窗口登录；登录后回到这里扫描即可"
            except Exception as exc:
                auth["message"] = public_error(exc)
            finally:
                auth["busy"] = False

        spawn("login", run())
    return auth


class ScanRequest(BaseModel):
    url: str = Field(min_length=1, max_length=5000)
    count: int = Field(default=50, ge=1, le=500)


@app.post("/api/scan")
async def scan_start(req: ScanRequest):
    if any(key.startswith("scan:") for key in tasks):
        raise HTTPException(409, "已有扫描正在进行，请等待完成或停止后再开始")
    try:
        url = clean_link(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    scan = {
        "id": uuid.uuid4().hex,
        "url": url,
        "target": req.count,
        "ids": [],
        "pages": 0,
        "status": "queued",
        "message": "等待扫描",
        "created": time.time(),
    }
    save("scan", scan)
    spawn("scan:" + scan["id"], scan_worker(scan))
    return scan


@app.get("/api/scans/{scan_id}")
async def get_scan(scan_id: str):
    scan = store.get("scan", scan_id)
    if not scan:
        raise HTTPException(404, "扫描记录不存在")
    items = [store.view(item_id, settings["output"]) for item_id in scan.get("ids", [])]
    scan["items"] = sorted([x for x in items if x], key=lambda x: x["published"], reverse=True)
    return scan


@app.post("/api/scans/{scan_id}/more")
async def scan_more(scan_id: str):
    if any(key.startswith("scan:") for key in tasks):
        raise HTTPException(409, "扫描仍在进行中")
    scan = store.get("scan", scan_id)
    if not scan or scan.get("type") != "user":
        raise HTTPException(400, "仅主页扫描可以继续加载")
    scan.update(target=len(scan.get("ids", [])) + 50, status="queued", message="等待继续加载")
    save("scan", scan)
    spawn("scan:" + scan_id, scan_worker(scan))
    return scan


@app.post("/api/scans/{scan_id}/stop")
async def scan_stop(scan_id: str):
    task = tasks.get("scan:" + scan_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        scan = store.get("scan", scan_id)
        if scan and scan.get("status") in {"queued", "running"}:
            scan.update(status="stopped", message="已停止扫描，保留已发现作品")
            save("scan", scan)
    return {"ok": True}


class DownloadRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@app.post("/api/download")
async def download(req: DownloadRequest):
    ids = list(dict.fromkeys(req.ids))
    for item_id in ids:
        item = store.raw(item_id)
        if not re.fullmatch(r"\d{10,25}", item_id) or not item or item.get("images"):
            raise HTTPException(400, "请选择已扫描的视频作品")
    jobs = []
    active = {(j["item_id"], j["output"]) for j in store.all("job") if j["status"] in {"queued", "running"}}
    for item_id in ids:
        if (item_id, settings["output"]) in active:
            continue
        job = {
            "id": uuid.uuid4().hex,
            "item_id": item_id,
            "title": store.raw(item_id).get("desc") or item_id,
            "output": settings["output"],
            "status": "queued",
            "message": "等待下载",
            "created": time.time(),
            "bytes_read": 0,
            "bytes_total": 0,
        }
        save("job", job)
        spawn("job:" + job["id"], download_worker(job))
        jobs.append(job)
    return {"jobs": jobs}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = store.get("job", job_id)
    if not job or job["status"] not in {"failed", "cancelled"}:
        raise HTTPException(400, "只有失败或取消的任务可以重试")
    if "job:" + job_id in tasks:
        raise HTTPException(409, "任务还在结束中，请稍后重试")
    job.update(status="queued", message="等待重试", bytes_read=0, bytes_total=0)
    save("job", job)
    spawn("job:" + job_id, download_worker(job))
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    task = tasks.get("job:" + job_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        job = store.get("job", job_id)
        if job and job.get("status") in {"queued", "running"}:
            job.update(status="cancelled", message="已取消下载")
            save("job", job)
    return {"ok": True}


class SettingsRequest(BaseModel):
    output: str = Field(min_length=1, max_length=240)


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    path = Path(req.output).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "请填写完整文件夹路径")
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / (".douyin-write-test-" + uuid.uuid4().hex)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        raise HTTPException(400, "该文件夹不可写，请选择其他位置")
    settings["output"] = str(path.resolve())
    store.put("settings", "main", settings)
    return settings


@app.post("/api/choose-folder")
async def choose_folder():
    def choose():
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askdirectory(
                title="选择视频保存文件夹", initialdir=settings["output"], parent=root
            )
        finally:
            root.destroy()

    result = await asyncio.to_thread(choose)
    if result:
        return await update_settings(SettingsRequest(output=result))
    return settings


@app.post("/api/open-folder")
async def open_folder():
    os.startfile(settings["output"])
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown():
    if server_instance:
        asyncio.get_running_loop().call_later(0.3, setattr, server_instance, "should_exit", True)
    return {"ok": True}


@app.get("/api/covers/{item_id}")
async def cover(item_id: str):
    # ID-based cache, never a user-supplied proxy URL.
    item = store.view(item_id, settings["output"])
    if not item or not item["cover"]:
        return Response(status_code=404)
    cover_dir = DATA / "covers"
    cover_dir.mkdir(exist_ok=True)
    cached = cover_dir / (item_id + ".img")
    if cached.exists():
        return FileResponse(cached, media_type="image/jpeg")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=12, follow_redirects=True, trust_env=False) as client:
            response = await client.get(item["cover"], headers={"Referer": "https://www.douyin.com/"})
            response.raise_for_status()
            if (
                not response.headers.get("content-type", "").startswith("image/")
                or len(response.content) > 8000000
            ):
                return Response(status_code=404)
            cached.write_bytes(response.content)
            return Response(response.content, media_type=response.headers["content-type"])
    except Exception:
        return Response(status_code=404)


if __name__ == "__main__":
    import uvicorn

    # A Proactor loop is required for Playwright subprocesses on Windows.
    async def serve():
        global server_instance
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, access_log=False))
        server_instance = server
        await server.serve()

    asyncio.run(serve())
