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

Failures are never silent. Every line carries an `event` name (`tracer_armed`,
`api_call`, `tracer_error`) so any of them can be filtered with grep. A failure
inside the tracer is recorded as `tracer_error` together with the exception, and
if even that write fails the message goes to stderr, where Ansible surfaces it
as a module warning. `tracer_armed` is written once per module process, which is
the per-process proof that tracing is alive. On top of that, the playbooks
assert that the journal grew during the run, so a trace that quietly stopped
working fails the run instead of leaving empty logs.

The one thing still swallowed on purpose: a tracer exception must never change
the outcome of the API call it wraps. The original request is always re-raised.
"""

import json
import os
import sys
import threading
import time

_TRACE_PATH = os.environ.get("PVE_HTTP_TRACE")
_SECRET_KEYS = ("password", "token", "secret", "key")

_lock = threading.Lock()


def _stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _to_stderr(message):
    """Last resort: a plain warning, which Ansible shows as a module warning."""
    try:
        print(f"[pve-tracer] {message}", file=sys.stderr)
    except Exception:
        pass


def _emit(entry):
    """Append one JSON line. Returns True on success, False if it could not."""
    try:
        directory = os.path.dirname(_TRACE_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        line = json.dumps(entry, default=str, sort_keys=True)
        with _lock, open(_TRACE_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 - the reason has to reach the operator
        _to_stderr(f"PVE_HTTP_TRACE write failed: {type(exc).__name__}: {exc}")
        return False


def _tracer_error(where, exc):
    """Record a tracer failure as an event; never let it escape."""
    _emit(
        {
            "ts": _stamp(),
            "event": "tracer_error",
            "pid": os.getpid(),
            "where": where,
            "error": f"{type(exc).__name__}: {exc}",
        }
    )


def _redact(body):
    """Drop secret-looking fields from a urlencoded or JSON request body."""
    try:
        if not body:
            return body
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                return "<binary request body>"
        if not isinstance(body, str):
            return body
        if "=" not in body:
            return body
        kept = []
        for part in body.split("&"):
            name = part.split("=", 1)[0].strip().lower()
            if any(secret in name for secret in _SECRET_KEYS):
                kept.append(f"{part.split('=', 1)[0]}=***redacted***")
            else:
                kept.append(part)
        return "&".join(kept)
    except Exception as exc:  # noqa: BLE001
        _tracer_error("redact", exc)
        return "<unredactable request body>"


def _arm():
    import requests  # imported inside so an ImportError is reported as an error

    # requests passes these positionally in places, so accept everything.
    _POSITIONAL = ("method", "url", "params", "data", "headers", "cookies", "files")
    _original_request = requests.Session.request

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
            _emit(
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
        except Exception as exc:  # noqa: BLE001
            _tracer_error("read_response_body", exc)
            response_text = "<unreadable response body>"
        _emit(
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


if _TRACE_PATH:
    try:
        _arm()
    except Exception as exc:  # noqa: BLE001 - arming failure has to be visible
        _to_stderr(f"tracing NOT armed: {type(exc).__name__}: {exc}")
        _emit(
            {
                "ts": _stamp(),
                "event": "tracer_error",
                "pid": os.getpid(),
                "where": "arm",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    else:
        # Per-process proof that tracing is alive: grep event=tracer_armed.
        _emit({"ts": _stamp(), "event": "tracer_armed", "pid": os.getpid()})
