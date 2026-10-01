import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, raw TEXT, fetched REAL);
            CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id));
        """)
        self.db.commit()

    def save_items(self, items):
        rows = [
            (str(x["aweme_id"]), json.dumps(x, ensure_ascii=False), time.time())
            for x in items
            if x.get("aweme_id")
        ]
        self.db.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?)", rows)
        self.db.commit()

    def raw(self, item_id):
        row = self.db.execute("SELECT raw FROM items WHERE id=?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, item_id, value):
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?)",
            (kind, item_id, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def get(self, kind, item_id, default=None):
        row = self.db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, item_id)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        return [
            json.loads(row[0]) for row in self.db.execute("SELECT value FROM records WHERE kind=?", (kind,))
        ]

    def view(self, item_id, output):
        raw = self.raw(item_id)
        if not raw:
            return None
        video = raw.get("video") or {}
        cover = video.get("cover") or video.get("origin_cover") or {}
        urls = cover.get("url_list") or []
        record = self.get("download", item_id, {})
        paths = record.get(str(Path(output).resolve()), [])
        exists = any(Path(p).is_file() and Path(p).stat().st_size > 0 for p in paths)
        return {
            "id": item_id,
            "title": raw.get("desc") or "未命名作品",
            "author": (raw.get("author") or {}).get("nickname") or "未知作者",
            "published": raw.get("create_time", 0),
            "duration": video.get("duration", 0),
            "cover": urls[0] if urls else "",
            "type": "gallery" if raw.get("images") else "video",
            "downloaded": exists,
            "url": "https://www.douyin.com/video/" + item_id,
        }

    def close(self):
        self.db.close()
