"""Check that this checkout can actually run, and say what is wrong if not.

    python scripts/doctor.py

Five checks, in the order they break: the hosted target, the state of the
member the shipped capabilities were recorded against, the model credentials,
the artifacts, and the service itself. Exits non-zero if anything failed.

Worth having because the failures here are not obvious from their symptoms. A
capability that stops on step three might mean a broken locator, or it might
mean somebody armed a fault on the shared host ten minutes ago; those want
different responses, and only one of them is yours to fix.

Python rather than a shell script on purpose: `bash` on Windows resolves to
either Git Bash or WSL depending on which wins the PATH, so a check that is
supposed to tell you whether things work should not itself be a thing that
can fail for reasons of its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://web-sample.interface-hiring.com"
PY = sys.executable
PORT = 8123

sys.path.insert(0, str(ROOT))

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    failures.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    print(f"  warn  {msg}")


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar())
    )


def fetch(op: urllib.request.OpenerDirector, url: str, data: dict[str, str] | None = None):
    """Return (status, body). An HTTP error status is an answer, not an exception."""
    body = urllib.parse.urlencode(data).encode() if data else None
    try:
        with op.open(urllib.request.Request(url, data=body), timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return 0, str(e)


# What each status means when the whole host starts returning it. The sample
# app can have any of its six faults armed globally from its System Settings
# screen, and when that happens every request gets the same answer. Naming the
# fault turns a mystery into a decision.
ARMED = {
    440: "session-expiry fault armed globally (System Settings)",
    503: "maintenance fault armed globally",
    500: "server fault armed globally",
    403: "permission fault armed globally",
    404: "not-found fault armed globally",
}


def check_target() -> bool:
    print("\n1. hosted target")
    # Retried: a globally armed fault can be cleared or set by anyone with
    # access to the console, so a single reading is not a verdict.
    status = 0
    for attempt in range(3):
        op = opener()
        fetch(op, f"{HOST}/signon", {"operator": "teller1", "password": "password"})
        status, _ = fetch(op, f"{HOST}/members")
        if status == 200:
            ok(f"reachable and authenticated (attempt {attempt + 1})")
            return True
        if attempt < 2:
            time.sleep(3)

    bad(f"/members returned {status} -- " + ARMED.get(status, "unexpected"))
    warn("the fault console at /settings is denied by this project's own allowlist,")
    warn("so this is not something a run can clear. Retry, or use the local target:")
    warn("  python -m target_app.app")
    warn("  python scripts/serve.py --policy config/policy.yaml")
    return False


def check_member(reachable: bool) -> None:
    print("\n2. member 102777 -- the record the shipped capabilities were recorded against")
    if not reachable:
        warn("skipped -- target not reachable")
        return
    r = subprocess.run([PY, "scripts/target_state.py"], cwd=ROOT, capture_output=True, text=True)
    print(r.stdout.rstrip() or "  (no output)")
    if r.returncode != 0:
        bad("could not read the member's shares")


def check_model_access() -> None:
    print("\n3. model credentials (no tokens spent)")
    code = (
        "import sys,pathlib;"
        "from dotenv import load_dotenv;"
        f"load_dotenv(pathlib.Path(r'{ROOT}')/'.env', override=True);"
        f"sys.path.insert(0, r'{ROOT / 'src'}');"
        "from cua.agent.client import build_client;"
        "print(len(list(build_client().models.list())))"
    )
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=ROOT)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        ok(f"{r.stdout.strip()} models reachable")
    else:
        last = (r.stderr.strip().splitlines() or [""])[-1][:140]
        bad(f"ANTHROPIC_API_KEY / ANTHROPIC_WORKSPACE_ID not working -- {last}")
        warn("only discovery and the chatbot's planner need this;")
        warn("replay works without it.")


def check_artifacts() -> None:
    print("\n4. artifacts")
    found = sorted((ROOT / "artifacts").glob("meridian_hosted*.json"))
    ok(f"{len(found)} hosted capabilities") if found else bad("no hosted artifacts found")
    if not found:
        return
    r = subprocess.run(
        [PY, "scripts/audit_capability.py", "--strict", *[str(f) for f in found]],
        capture_output=True, text=True, cwd=ROOT,
    )
    if r.returncode == 0:
        ok("audit --strict clean -- no checkpoint looks pinned to one record")
    else:
        bad("audit --strict flagged a capability; run it directly to see which")


def check_service() -> None:
    print("\n5. service")
    proc = subprocess.Popen(
        [PY, "scripts/serve.py", "--headless", "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        health = None
        for _ in range(20):
            time.sleep(1)
            status, body = fetch(opener(), f"http://127.0.0.1:{PORT}/health")
            if status == 200:
                health = json.loads(body)
                break
        if not health:
            bad("did not start -- run `python scripts/serve.py` to see why")
            return
        ok(f"{health['capabilities']} capabilities loaded, policy {health['policy']}")
        for page in ("/ui", "/ui/chat", "/ui/runs", "/docs"):
            status, _ = fetch(opener(), f"http://127.0.0.1:{PORT}{page}")
            ok(page) if status == 200 else bad(f"{page} returned {status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    reachable = check_target()
    check_member(reachable)
    check_model_access()
    check_artifacts()
    check_service()

    print()
    if failures:
        print(f"  {len(failures)} check(s) failed:")
        for f in failures:
            print(f"    - {f}")
        print()
        return 1
    print("  all checks passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
