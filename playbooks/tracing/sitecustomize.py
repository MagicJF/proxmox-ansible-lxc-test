"""HTTP journal for every Proxmox API call made from an Ansible module process.

How it gets loaded: the playbooks put this directory on PYTHONPATH and set
PVE_HTTP_TRACE to the target file. CPython imports `sitecustomize` automatically
at interpreter start-up, so the trace is armed in every module process before
the module runs — no change to the modules themselves.

What is recorded: one JSON object per HTTP call, with the method, the URL, the
request body and the PVE response (status, body, duration). This is the real
traffic, including the parameters PVE rejects and the error bodies it returns —
exactly what Ansible never shows you because the tasks carry `no_log`.

What is deliberately NOT recorded: request headers, so the API token never
reaches the file. Anything in the request body whose key looks like a secret is
redacted as well.

Tracing must never break a run: every failure path here is swallowed.
"""

import json
import os
import threading
import time

_TRACE_PATH = os.environ.get("PVE_HTTP_TRACE")
_SECRET_KEYS = ("password", "token", "secret", "key")


def _redact(body):
    """Drop secret-looking fields from a urlencoded or JSON request body."""
    if not body:
        return body
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return "<binary request body>"
    if not isinstance(body, str):
        return body
    separator = "&" if "=" in body else None
    if not separator:
        return body
    kept = []
    for part in body.split(separator):
        name = part.split("=", 1)[0].strip().lower()
        if any(secret in name for secret in _SECRET_KEYS):
            kept.append(f"{part.split('=', 1)[0]}=***redacted***")
        else:
            kept.append(part)
    return separator.join(kept)


if _TRACE_PATH:
    try:
        import requests

        _lock = threading.Lock()

        def _record(entry):
            try:
                directory = os.path.dirname(_TRACE_PATH)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                line = json.dumps(entry, default=str, sort_keys=True)
                with _lock, open(_TRACE_PATH, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:
                pass

        def _stamp():
            return time.strftime("%Y-%m-%dT%H:%M:%S%z")

        _original_request = requests.Session.request

        # requests passes this positionally in places, so accept everything.
        _POSITIONAL = ("method", "url", "params", "data", "headers", "cookies", "files")

        def _traced_request(self, *args, **kwargs):
            bound = dict(zip(_POSITIONAL, args))
            method = bound.get("method") or kwargs.get("method")
            url = bound.get("url") or kwargs.get("url")
            body = bound.get("data", kwargs.get("data"))
            if body is None:
                body = kwargs.get("json")
            started = time.time()
            try:
                response = _original_request(self, *args, **kwargs)
            except Exception as exc:
                _record(
                    {
                        "ts": _stamp(),
                        "event": "api_call",
                        "pid": os.getpid(),
                        "method": method,
                        "url": url,
                        "request_body": _redact(body),
                        "error": f"{type(exc).__name__}: {exc}",
                        "seconds": round(time.time() - started, 3),
                    }
                )
                raise
            try:
                response_text = response.text
            except Exception:
                response_text = "<unreadable response body>"
            _record(
                {
                    "ts": _stamp(),
                    "event": "api_call",
                    "pid": os.getpid(),
                    "method": method,
                    "url": url,
                    "request_body": _redact(body),
                    "status": response.status_code,
                    "response_body": response_text,
                    "seconds": round(time.time() - started, 3),
                }
            )
            return response

        requests.Session.request = _traced_request
    except Exception:
        pass
