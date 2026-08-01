from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BarkError(RuntimeError):
    pass


@dataclass(frozen=True)
class BarkNotifier:
    server_url: str
    device_key: str
    group: str = "LongWatch"
    level: str = "timeSensitive"
    sound: str = ""
    timeout_seconds: float = 10

    def send(self, title: str, body: str, url: str = "") -> None:
        payload: dict[str, object] = {
            "device_key": self.device_key,
            "title": title,
            "body": body,
            "group": self.group,
            "level": self.level,
        }
        if self.sound:
            payload["sound"] = self.sound
        if url:
            payload["url"] = url
        request = Request(
            f"{self.server_url.rstrip('/')}/push",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BarkError(f"Bark HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise BarkError(f"无法连接 Bark: {exc.reason}") from exc
        if status < 200 or status >= 300:
            raise BarkError(f"Bark HTTP {status}: {response_body}")
        try:
            result = json.loads(response_body)
        except json.JSONDecodeError:
            return
        if isinstance(result, dict) and result.get("code") not in (None, 200):
            raise BarkError(f"Bark 推送失败: {result}")
