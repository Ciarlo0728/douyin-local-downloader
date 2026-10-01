import json
import re
import urllib.request

url = "http://127.0.0.1:18765"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(url + "/api/health", timeout=2) as response:
        if json.load(response).get("app") != "douyin-local":
            raise RuntimeError("Different service")
    with opener.open(url, timeout=2) as response:
        html = response.read().decode("utf-8")
    token = re.search('name="local-token" content="([^"]+)"', html).group(1)
    request = urllib.request.Request(
        url + "/api/shutdown",
        data=b"{}",
        method="POST",
        headers={"X-Local-Token": token, "Content-Type": "application/json"},
    )
    opener.open(request, timeout=5).close()
except Exception:
    pass
