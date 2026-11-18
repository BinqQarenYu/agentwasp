#!/usr/bin/env python3
"""waspctl — zero-dependency CLI for Agent WASP teammates.

Works inside any CLI harness (Claude Code, Codex, Pi, Gemini) or shell skill because
it uses only Python's standard library and talks to Agent WASP over local HTTP using
the per-run `WASP_API`, `WASP_TOKEN`, and `WASP_BOT_ID` environment variables.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

HELP = """waspctl: coordinate teammates, routines, and human handoffs inside Agent WASP.

  waspctl bots                                          List all active bots
  waspctl ask "<Bot name or id>" "<message>" [--timeout 120]
                                                        Ask another bot; prints its reply
  waspctl approve "<action>" "<summary>" [--detail "..."] [--origin user_direct|routine|external_untrusted] [--timeout 1800]
                                                        Gate a risky action through Auto Review / Approval Card
  waspctl handoff "<what you need>" [--timeout 600]     Ask the human operator for help; waits for Done
  waspctl screen                                        Ensure and print this bot's live screen info/URL
  waspctl routines                                      List scheduled routines
  waspctl routine add "<name>" "<interval>" "<what>"    Create a recurring routine (e.g. "every 1h", "daily")
  waspctl routine pause <id> | resume <id>              Pause or resume a routine
  waspctl routine rm <id>                               Delete a routine
"""


def _api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    base = (os.environ.get("WASP_API") or "").rstrip("/")
    token = os.environ.get("WASP_TOKEN") or ""
    if not base or not token:
        raise RuntimeError(
            "waspctl only works inside an authenticated bot run (WASP_API and WASP_TOKEN are missing)."
        )

    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace").strip()
        try:
            err_payload = json.loads(raw)
            msg = err_payload.get("error") or err_payload.get("detail") or raw
        except Exception:
            msg = raw or exc.reason
        raise RuntimeError(f"{msg} (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach Agent WASP API at {base}: {exc.reason}") from exc


def _extract_timeout(args: list[str], default: float) -> tuple[list[str], float]:
    cleaned: list[str] = []
    timeout = default
    i = 0
    while i < len(args):
        if args[i] == "--timeout" and i + 1 < len(args):
            timeout = float(args[i + 1])
            i += 2
        else:
            cleaned.append(args[i])
            i += 1
    return cleaned, timeout


def run_cli(argv: list[str]) -> str:
    if not argv or argv[0] in ("-h", "--help", "help"):
        return HELP.strip()

    bot_id = os.environ.get("WASP_BOT_ID", "")
    cmd = argv[0]

    if cmd == "bots":
        res = _api("GET", "/bots", timeout=30.0)
        return json.dumps(res.get("bots", res), indent=2)

    if cmd == "ask":
        rest, timeout = _extract_timeout(argv[1:], default=120.0)
        if len(rest) < 2:
            raise RuntimeError('Usage: waspctl ask "<Bot name or id>" "<message>" [--timeout 120]')
        target, message = rest[0], rest[1]
        res = _api(
            "POST",
            "/ask",
            body={"from": bot_id, "to": target, "text": message, "timeout": timeout},
            timeout=timeout + 15.0,
        )
        return str(res.get("reply") or json.dumps(res, indent=2))

    if cmd == "approve":
        args = argv[1:]
        pos: list[str] = []
        detail = ""
        origin = "user_direct"
        timeout: float | None = None
        i = 0
        while i < len(args):
            if args[i] == "--detail" and i + 1 < len(args):
                detail = args[i + 1]
                i += 2
            elif args[i] == "--origin" and i + 1 < len(args):
                origin = args[i + 1]
                i += 2
            elif args[i] == "--timeout" and i + 1 < len(args):
                timeout = float(args[i + 1])
                i += 2
            else:
                pos.append(args[i])
                i += 1
        if len(pos) < 2:
            raise RuntimeError(
                'Usage: waspctl approve "<action>" "<summary>" [--detail "..."] [--origin user_direct|routine|external_untrusted] [--timeout 1800]'
            )
        action_name, summary = pos[0], pos[1]
        payload: dict[str, Any] = {
            "from": bot_id,
            "action_name": action_name,
            "action_summary": summary,
            "action_detail": detail,
            "origin": origin,
        }
        if timeout is not None:
            payload["timeout"] = timeout
        res = _api("POST", "/approve", body=payload, timeout=(timeout or 1800.0) + 15.0)
        if not res.get("approved"):
            raise RuntimeError(res.get("reason") or res.get("status") or "Action denied")
        return str(res.get("reason") or "Approved")

    if cmd == "handoff":
        rest, timeout = _extract_timeout(argv[1:], default=600.0)
        if not rest:
            raise RuntimeError('Usage: waspctl handoff "<what you need>" [--timeout 600]')
        reason = " ".join(rest)
        res = _api(
            "POST",
            "/handoff",
            body={"from": bot_id, "text": reason, "timeout": timeout},
            timeout=timeout + 15.0,
        )
        return str(res.get("note") or res.get("status") or json.dumps(res))

    if cmd == "screen":
        res = _api("POST", "/screen", body={"from": bot_id}, timeout=30.0)
        return json.dumps(res.get("screen", res), indent=2)

    if cmd == "routines" or (cmd == "routine" and len(argv) >= 2 and argv[1] == "list"):
        res = _api("GET", "/routines", timeout=30.0)
        return json.dumps(res.get("routines", res), indent=2)

    if cmd == "routine" and len(argv) >= 2:
        sub = argv[1]
        if sub == "add" and len(argv) >= 5:
            name, schedule, instruction = argv[2], argv[3], argv[4]
            res = _api(
                "POST",
                "/routines",
                body={"bot_id": bot_id, "name": name, "schedule": schedule, "instruction": instruction},
                timeout=30.0,
            )
            rid = res.get("id") or res.get("routine", {}).get("id", "")
            return f'Added routine "{name}" ({schedule}) [id: {rid}].'
        if sub in ("pause", "resume") and len(argv) >= 3:
            rid = argv[2]
            _api("PATCH", f"/routines/{rid}", body={"enabled": sub == "resume"}, timeout=30.0)
            return "Resumed." if sub == "resume" else "Paused."
        if sub in ("rm", "delete") and len(argv) >= 3:
            rid = argv[2]
            _api("DELETE", f"/routines/{rid}", timeout=30.0)
            return "Deleted."

    return HELP.strip()


def main(argv: list[str] | None = None) -> int:
    try:
        output = run_cli(sys.argv[1:] if argv is None else argv)
        if output:
            print(output)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
