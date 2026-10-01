import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DOUYIN_LOCAL_DATA"] = tempfile.mkdtemp(prefix="douyin-tests-", dir=ROOT.parents[1] / "work")
local = importlib.import_module("app")
from browser_gateway import BrowserGateway
from store import Store


def item(number):
    return {
        "aweme_id": str(number),
        "desc": "测试作品",
        "create_time": 100,
        "author": {"nickname": "测试作者"},
        "video": {"duration": 123000},
    }


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db = Store(tmp_path / "test.sqlite3")
    monkeypatch.setattr(local, "store", db)
    monkeypatch.setattr(local, "settings", {"output": str(tmp_path / "downloads")})
    monkeypatch.setattr(local, "tasks", {})
    monkeypatch.setattr(local, "slots", asyncio.Semaphore(2))
    monkeypatch.setattr(local, "download_lock", {})
    monkeypatch.setattr(local, "scan_lock", asyncio.Lock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=local.app),
        base_url="http://127.0.0.1",
        headers={"X-Local-Token": local.TOKEN},
    ) as session:
        yield session
    for task in list(local.tasks.values()):
        task.cancel()
    await asyncio.gather(*list(local.tasks.values()), return_exceptions=True)
    db.close()


async def test_local_token_and_foreign_url_rejected(client):
    response = await client.post(
        "/api/download", json={"ids": ["1234567890123456"]}, headers={"X-Local-Token": "wrong"}
    )
    assert response.status_code == 403
    for bad in (
        "https://evil.example/video/1234567890",
        "https://www.douyin.com.evil.example/video/1234567890",
        "https://u:p@www.douyin.com/video/1234567890",
    ):
        response = await client.post("/api/scan", json={"url": bad})
        assert response.status_code == 400
    assert not local.tasks


def test_share_text_and_profile_query():
    url = local.clean_link("来看看这个 https://www.douyin.com/user/MS4w-test?vid=1234567890 复制此链接")
    assert local.URLParser.parse(url)["type"] == "user"
    assert (
        local.clean_link("[https://v.douyin.com/abcd/](https://v.douyin.com/abcd/)")
        == "https://v.douyin.com/abcd/"
    )


async def test_selected_items_only_and_duplicate_submission(client, monkeypatch):
    ids = ["1234567890123456", "1234567890123457", "1234567890123458"]
    local.store.save_items([item(n) for n in ids])
    gate = asyncio.Event()
    called = []

    async def fake(job):
        called.append(job["item_id"])
        await gate.wait()

    monkeypatch.setattr(local, "download_worker", fake)
    response = await client.post("/api/download", json={"ids": [ids[0], ids[2], ids[0]]})
    assert response.status_code == 200
    await asyncio.sleep(0)
    assert set(called) == {ids[0], ids[2]}
    again = await client.post("/api/download", json={"ids": [ids[0]]})
    assert again.json()["jobs"] == []
    assert ids[1] not in called
    gate.set()


async def test_cancel_queued_job_persists_state(client, monkeypatch):
    local.store.save_items([item("1234567890123456")])
    monkeypatch.setattr(local, "download_worker", AsyncMock(side_effect=lambda job: None))
    # Explicitly schedule a coroutine that has not entered the worker's cancellation handler.
    job = {
        "id": "queued",
        "item_id": "1234567890123456",
        "status": "queued",
        "output": local.settings["output"],
    }
    local.save("job", job)
    local.spawn("job:queued", asyncio.sleep(100))
    response = await client.post("/api/jobs/queued/cancel", json={})
    assert response.status_code == 200
    assert local.store.get("job", "queued")["status"] == "cancelled"


async def test_disk_skip_does_not_need_network_and_deleted_file_retries(client, tmp_path, monkeypatch):
    video = tmp_path / "existing.mp4"
    video.write_bytes(b"test media bytes")
    video_id = "1234567890123456"
    local.store.save_items([item(video_id)])
    local.store.put("download", video_id, {str(Path(local.settings["output"]).resolve()): [str(video)]})
    job = {"id": "skip", "item_id": video_id, "output": local.settings["output"], "status": "queued"}
    await local.download_worker(job)
    assert local.store.get("job", "skip")["status"] == "skipped"
    video.unlink()
    assert local.find_downloaded(local.settings["output"], video_id) == []
    assert not local.store.view(video_id, local.settings["output"])["downloaded"]


async def test_browser_response_scope_and_precision(tmp_path):
    captured = []
    gateway = BrowserGateway(tmp_path, captured.extend)
    gateway.profile_id = "MS4w-correct"

    class Reply:
        status = 200
        url = "https://www-hj.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=MS4w-wrong&max_cursor=0"

        async def json(self):
            return {
                "status_code": 0,
                "aweme_list": [item("7681997810148969743")],
                "has_more": 1,
                "max_cursor": 99,
            }

    response = Reply()
    await gateway._consume(response)
    assert not captured
    response.url = response.url.replace("MS4w-wrong", "MS4w-correct")
    await gateway._consume(response)
    assert captured[0]["aweme_id"] == "7681997810148969743"
    assert gateway.pages[0]["next"] == "99"


async def test_scan_failure_preserves_discovered_items(client, monkeypatch):
    fake = AsyncMock()

    async def broken_scan(uid, target, ids, progress):
        local.store.save_items([item("1234567890123456")])
        progress(["1234567890123456"], 1)
        raise RuntimeError("需要重新登录")

    fake.scan = broken_scan
    monkeypatch.setattr(local, "browser", fake)
    scan = {"id": "partial", "url": "https://www.douyin.com/user/MS4w-test", "target": 50, "ids": []}
    await local.scan_worker(scan)
    result = (await client.get("/api/scans/partial")).json()
    assert result["status"] == "restricted"
    assert len(result["items"]) == 1
    assert result["message"] == "需要重新登录"


async def test_retry_only_failed_or_cancelled(client, monkeypatch):
    local.save("job", {"id": "finished", "status": "success"})
    assert (await client.post("/api/jobs/finished/retry", json={})).status_code == 400

    async def wait(job):
        await asyncio.sleep(100)

    monkeypatch.setattr(local, "download_worker", wait)
    local.save("job", {"id": "failed", "status": "failed"})
    result = await client.post("/api/jobs/failed/retry", json={})
    assert result.json()["status"] == "queued"


async def test_settings_require_absolute_path(client):
    assert (await client.post("/api/settings", json={"output": "relative/path"})).status_code == 400
