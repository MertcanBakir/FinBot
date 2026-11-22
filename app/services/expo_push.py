"""Bounded Expo HTTP adapter. Never expose provider response bodies in errors."""

import json
import urllib.error
import urllib.request


class ExpoPush:
    def __init__(self, access_token=None):
        self.access_token = access_token

    def request(self, route, payload):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        request = urllib.request.Request(
            "https://exp.host/--/api/v2/push/" + route,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read(65536))
        except urllib.error.HTTPError as exc:
            return {
                "failure": "retry"
                if exc.code == 429 or exc.code >= 500
                else "permanent"
            }
        except Exception:
            return {"failure": "unknown"}

    def send(self, item):
        payload = {
            "to": item["token"],
            "title": item["title"],
            "body": item["body"],
            "data": {**item["data"], "notification_id": str(item["notification_id"])},
            "sound": "default",
            "expiration": int(item["expires_at"].timestamp()),
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 3800:
            return {"failure": "permanent"}
        return self.request("send", payload)

    def receipt(self, ticket):
        return self.request("getReceipts", {"ids": [ticket]})
