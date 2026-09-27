"""Small, dependency-free ntfy client used by DoseKeep's reminder worker."""

from urllib.parse import quote
from urllib.request import Request, urlopen


def send_ntfy(
    server_url: str,
    topic: str,
    title: str,
    message: str,
    tags: str = "pill",
    click_url: str | None = None,
) -> None:
    """Send a notification to a configured ntfy topic.

    The caller owns validation and retry policy. A non-2xx response raises so
    a dose is not marked as notified until ntfy has accepted it.
    """
    url = f"{server_url.rstrip('/')}/{quote(topic, safe='')}"
    headers = {"Title": title, "Tags": tags, "Priority": "default"}
    if click_url:
        headers["Click"] = click_url
    request = Request(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ntfy returned HTTP {response.status}")
