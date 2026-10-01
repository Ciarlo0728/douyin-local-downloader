"""Use a separate, persistent Edge profile; collect actual Douyin page responses."""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from playwright.async_api import async_playwright


class BrowserGateway:
    def __init__(self, data_dir: Path, on_items):
        self.data_dir = data_dir
        self.on_items = on_items
        self.pw = self.context = self.page = None
        self.lock = asyncio.Lock()
        self.pending = set()
        self.profile_id = ""
        self.pages = []
        self.details = {}
        self.items = {}
        self.last_error = ""

    async def ensure(self):
        if self.context:
            try:
                await self.context.cookies()
                if not self.page or self.page.is_closed():
                    self.page = await self.context.new_page()
                return
            except Exception:
                self.context = None
        if self.pw is None:
            self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            str(self.data_dir / "browser"),
            channel="msedge",
            headless=False,
            locale="zh-CN",
            viewport={"width": 1280, "height": 860},
            accept_downloads=False,
        )
        self.context.on("response", self._response)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    def _response(self, response):
        path = urlsplit(response.url).path
        if path not in ("/aweme/v1/web/aweme/post/", "/aweme/v1/web/aweme/detail/"):
            return
        task = asyncio.create_task(self._consume(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _consume(self, response):
        try:
            raw = await response.json()
            params = parse_qs(urlsplit(response.url).query)
            if not isinstance(raw, dict):
                return
            detail = raw.get("aweme_detail")
            if isinstance(detail, dict) and detail.get("aweme_id"):
                self.details[str(detail["aweme_id"])] = detail
                self.on_items([detail])
            if "/aweme/post/" in response.url and params.get("sec_user_id", [""])[0] == self.profile_id:
                batch = [x for x in (raw.get("aweme_list") or []) if isinstance(x, dict)]
                self.pages.append(
                    {
                        "cursor": params.get("max_cursor", ["0"])[0],
                        "next": str(raw.get("max_cursor", 0)),
                        "more": bool(raw.get("has_more")),
                        "count": len(batch),
                        "code": raw.get("status_code"),
                        "http": response.status,
                    }
                )
                for item in batch:
                    if item.get("aweme_id"):
                        self.items[str(item["aweme_id"])] = item
                self.on_items(batch)
        except Exception:
            self.last_error = "浏览器接口未返回可读取的作品数据"

    async def cookies(self):
        if not self.context:
            return {}
        try:
            return {c["name"]: c["value"] for c in await self.context.cookies("https://www.douyin.com")}
        except Exception:
            return {}

    async def status(self):
        cookies = await self.cookies()
        return {
            "open": bool(self.context and self.page and not self.page.is_closed()),
            "logged_in": bool(cookies.get("sessionid") or cookies.get("sessionid_ss")),
        }

    async def login(self):
        async with self.lock:
            await self.ensure()
            await self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            await self.page.bring_to_front()

    async def scan(self, uid, target, previous_ids, progress):
        async with self.lock:
            await self.ensure()
            if uid != self.profile_id or "/user/" + uid not in self.page.url:
                self.profile_id, self.pages, self.items = uid, [], {}
                await self.page.goto(
                    "https://www.douyin.com/user/" + uid, wait_until="domcontentloaded", timeout=60000
                )
            baseline = set(previous_ids)
            started = time.monotonic()
            last_count, idle = -1, 0
            while time.monotonic() - started < 110:
                await asyncio.sleep(1.5)
                ids = baseline | set(self.items)
                progress(list(ids), len(self.pages))
                if len(ids) >= target:
                    return "limited", "达到本次扫描数量，可继续加载", list(ids)
                if self.pages and self.pages[-1]["code"] == 0 and not self.pages[-1]["more"]:
                    # Only a real later page with a non-stalled cursor can establish an end.
                    if len(self.pages) > 1 and self.pages[-1]["cursor"] != "0":
                        return "complete", "接口已返回末页；仅包含当前可见作品", list(ids)
                if len(ids) == last_count:
                    idle += 1
                else:
                    idle = 0
                last_count = len(ids)
                if idle >= 12:
                    break
                await self.page.mouse.move(980, 660)
                await self.page.mouse.wheel(0, 1800)
                # Scroll the page's actual scroll container as well as the viewport.
                await self.page.evaluate("""() => {
                    for (const e of document.querySelectorAll('main, [data-e2e="user-post-list"], div')) {
                        const s = getComputedStyle(e);
                        if (e.clientHeight > 200 && e.scrollHeight > e.clientHeight + 100 &&
                            ['auto','scroll'].includes(s.overflowY)) e.scrollTop += 1200;
                    }
                }""")
            return (
                "restricted",
                "扫描暂时停住：请在抖音窗口完成登录或验证，再继续加载；尚未确认抓全",
                list(baseline | set(self.items)),
            )

    async def detail(self, aweme_id):
        if aweme_id in self.details:
            return self.details[aweme_id]
        if aweme_id in self.items:
            return self.items[aweme_id]
        async with self.lock:
            await self.ensure()
            await self.page.goto(
                "https://www.douyin.com/video/" + aweme_id, wait_until="domcontentloaded", timeout=60000
            )
            for _ in range(12):
                await asyncio.sleep(1)
                if aweme_id in self.details:
                    return self.details[aweme_id]
            # Some page versions embed the detail rather than issuing a detail request.
            scripts = await self.page.locator("script#RENDER_DATA, script#__NEXT_DATA__").all_text_contents()
            for text in scripts:
                try:
                    found = self._find_detail(json.loads(unquote(text)), aweme_id)
                    if found:
                        self.details[aweme_id] = found
                        self.on_items([found])
                        return found
                except (ValueError, TypeError):
                    pass
            raise RuntimeError("未读取到视频详情。请在抖音窗口确认视频可播放并完成登录或验证，再重试。")

    @classmethod
    def _find_detail(cls, value, aweme_id):
        if isinstance(value, dict):
            if str(value.get("aweme_id", "")) == aweme_id and value.get("video"):
                return value
            for child in value.values():
                found = cls._find_detail(child, aweme_id)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_detail(child, aweme_id)
                if found:
                    return found

    async def resolve(self, url):
        async with self.lock:
            await self.ensure()
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(10):
                if urlsplit(self.page.url).hostname == "www.douyin.com":
                    return self.page.url
                await asyncio.sleep(1)
            return self.page.url

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)
        if self.pw:
            await self.pw.stop()
