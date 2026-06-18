#!/usr/bin/env python3
"""coolify.py — drive a self-hosted Coolify (v4, API v1) from the command line.

Stdlib only. Creds from env (COOLIFY_BASE_URL, COOLIFY_API_TOKEN) or a sibling .env;
NEVER stored on disk. Writes are dry-run by default — add --apply to perform; deletes
also need --confirm <resource-name>.

Run `python coolify.py --self-test` to run the offline test suite.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path


# ── errors ──────────────────────────────────────────────────────────────────
class CoolifyError(Exception):
    pass


class ConfigError(CoolifyError):
    pass


class ConfirmError(CoolifyError):
    pass


class APIError(CoolifyError):
    def __init__(self, status, body, hint=""):
        self.status, self.body, self.hint = status, body, hint
        super().__init__(f"HTTP {status}: {hint or (body[:200] if body else '')}")


# ── config ──────────────────────────────────────────────────────────────────
def load_env(start: Path):
    for d in [start, *start.parents]:
        envf = d / ".env"
        if envf.is_file():
            for line in envf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break


def load_config(start: Path | None = None):
    load_env(start or Path.cwd())
    base = os.environ.get("COOLIFY_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("COOLIFY_API_TOKEN", "").strip()
    if not base or not token:
        raise ConfigError(
            "Set COOLIFY_BASE_URL and COOLIFY_API_TOKEN (env or .env). "
            "Base looks like https://<instance>/api/v1; token from Coolify → "
            "Keys & Tokens → API Tokens."
        )
    if not base.endswith("/api/v1"):
        print(f"warning: COOLIFY_BASE_URL usually ends with /api/v1 (got {base})",
              file=sys.stderr)
    return base, token


# ── transport ───────────────────────────────────────────────────────────────
def real_transport(method, url, headers, body):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


HINTS = {
    401: "Token invalid or missing ability (need read/write/deploy; read:sensitive for env values).",
    403: "Token lacks the required ability for this action.",
    404: "Resource not found — check the uuid.",
    409: "Conflict (often a domain already in use) — retry with force_domain_override=true.",
    422: "Validation failed — wrong app-create variant or missing required fields.",
    429: "Rate limited / deploy queue saturated.",
}

MUTATING = {"POST", "PATCH", "PUT", "DELETE"}


class DryRun:
    def __init__(self, method, path, body):
        self.method, self.path, self.body = method, path, body

    def __str__(self):
        b = f" body={json.dumps(self.body)}" if self.body is not None else ""
        return f"WOULD {self.method} {self.path}{b}  (re-run with --apply to perform)"

    def to_dict(self):
        return {"dry_run": True, "method": self.method, "path": self.path, "body": self.body}


class CoolifyClient:
    def __init__(self, base_url, token, apply=False, transport=None,
                 sleeper=time.sleep, max_retries=4):
        self.base = base_url.rstrip("/")
        self.token = token
        self.apply = apply
        self.transport = transport or real_transport
        self.sleeper = sleeper
        self.max_retries = max_retries

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(self, method, path, body=None, *, destructive=False,
                confirm=None, resource_name=None):
        method = method.upper()
        if method in MUTATING:
            if not self.apply:
                return DryRun(method, path, body)
            if destructive and confirm != resource_name:
                raise ConfirmError(
                    f"Destructive {method} {path} needs --confirm matching the resource "
                    f"name '{resource_name}' (got {confirm!r})."
                )
        url = self.base + path
        payload = json.dumps(body) if body is not None else None
        attempt = 0
        while True:
            status, headers, raw = self.transport(method, url, self._headers(), payload)
            if status == 429 and attempt < self.max_retries:
                wait = float(headers.get("Retry-After", 2) or 2)
                attempt += 1
                self.sleeper(wait)
                continue
            if status >= 400:
                text = raw.decode(errors="replace") if raw else ""
                raise APIError(status, text, HINTS.get(status, ""))
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw.decode(errors="replace")}

    def get(self, p, **kw):
        return self.request("GET", p, **kw)

    def post(self, p, body=None, **kw):
        return self.request("POST", p, body, **kw)

    def patch(self, p, body=None, **kw):
        return self.request("PATCH", p, body, **kw)

    def delete(self, p, **kw):
        return self.request("DELETE", p, **kw)


# ── deployment state machine ──────────────────────────────────────────────────
TERMINAL_SUCCESS = {"finished"}
TERMINAL_FAILURE = {"failed", "cancelled-by-user"}


def classify_status(s):
    if s in TERMINAL_SUCCESS:
        return "success"
    if s in TERMINAL_FAILURE:
        return "failure"
    return "running"  # queued / in_progress / unknown → keep polling


def wait_for_deployment(client, dep_uuid, timeout=600, interval=3, sleeper=None):
    sleeper = sleeper or client.sleeper
    waited = 0
    dep = {}
    while True:
        dep = client.get(f"/deployments/{dep_uuid}")
        if classify_status(dep.get("status", "")) != "running":
            return dep
        if waited >= timeout:
            dep["_timed_out"] = True
            return dep
        sleeper(interval)
        waited += interval


# ── resource routing tables ───────────────────────────────────────────────────
LIST_PATHS = {
    "apps": "/applications",
    "db": "/databases",
    "svc": "/services",
    "server": "/servers",
    "project": "/projects",
}
ITEM_PREFIX = dict(LIST_PATHS)

APP_CREATE = {
    "public": "/applications/public",
    "private-github-app": "/applications/private-github-app",
    "private-deploy-key": "/applications/private-deploy-key",
    "dockerfile": "/applications/dockerfile",
    "dockerimage": "/applications/dockerimage",
}
DB_ENGINES = {e: f"/databases/{e}" for e in (
    "postgresql", "mysql", "mariadb", "mongodb",
    "redis", "keydb", "dragonfly", "clickhouse")}
ENV_BASE = {"app": "/applications", "db": "/databases", "svc": "/services"}


def _read_json_arg(src):
    text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    return json.loads(text)


def parse_env_file(text):
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(prog="coolify.py", description="Drive a Coolify instance.")
    p.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    p.add_argument("--confirm", default=None, help="resource name, required for destructive ops")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--self-test", action="store_true", help="run the offline test suite and exit")
    sub = p.add_subparsers(dest="group")

    sub.add_parser("ping")
    sub.add_parser("resources")

    for g in ("apps", "db", "svc", "server", "project"):
        gp = sub.add_parser(g).add_subparsers(dest="action")
        gp.add_parser("list")
        gp.add_parser("get").add_argument("uuid")
        gp.add_parser("delete").add_argument("uuid")
        for life in ("start", "stop", "restart"):
            gp.add_parser(life).add_argument("uuid")

    # read-only extras
    apps = _find_sub(p, "apps")
    apps.add_parser("logs").add_argument("uuid")
    srv = _find_sub(p, "server")
    srv.add_parser("validate").add_argument("uuid")
    srv.add_parser("resources").add_argument("uuid")

    dep = sub.add_parser("deployment").add_subparsers(dest="action")
    dep.add_parser("get").add_argument("uuid")
    dep.add_parser("history").add_argument("app_uuid")

    # create verbs (variant/engine aware)
    appc = _find_sub(p, "apps").add_parser("create")
    appc.add_argument("variant", choices=sorted(APP_CREATE))
    appc.add_argument("--json-body", required=True, help="path to JSON body, or - for stdin")
    dbc = _find_sub(p, "db").add_parser("create")
    dbc.add_argument("engine", choices=sorted(DB_ENGINES))
    dbc.add_argument("--json-body", required=True, help="path to JSON body, or - for stdin")

    env = sub.add_parser("env").add_subparsers(dest="action")
    for name in ("list", "set", "set-bulk", "delete"):
        ep = env.add_parser(name)
        ep.add_argument("--target", choices=("app", "db", "svc"), default="app")
        ep.add_argument("uuid")
        if name == "set":
            ep.add_argument("pair", help="KEY=VALUE")
        if name == "set-bulk":
            ep.add_argument("file", help="path to .env-style or JSON file")
        if name == "delete":
            ep.add_argument("env_uuid")

    dp = sub.add_parser("deploy")
    dp.add_argument("uuid", nargs="?")
    dp.add_argument("--tag", default=None)
    dp.add_argument("--force", action="store_true")

    # extend the existing `deployment` subparser created above:
    depsub = _find_sub(p, "deployment")
    w = depsub.add_parser("wait")
    w.add_argument("uuid")
    w.add_argument("--timeout", type=int, default=600)

    return p


def _find_sub(parser, name):
    """Return the subparsers object attached to subcommand `name`."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            target = action.choices.get(name)
            if target is None:
                raise KeyError(name)
            for a in target._actions:
                if isinstance(a, argparse._SubParsersAction):
                    return a
    raise KeyError(name)


def dispatch(client, args):
    g = args.group
    if g == "ping":
        return client.get("/version")
    if g == "resources":
        return client.get("/resources")
    if g in LIST_PATHS:
        act = args.action
        if act == "list":
            return client.get(LIST_PATHS[g])
        if act == "get":
            return client.get(f"{ITEM_PREFIX[g]}/{args.uuid}")
        if g == "apps" and act == "logs":
            return client.get(f"/applications/{args.uuid}/logs")
        if g == "server" and act == "validate":
            return client.get(f"/servers/{args.uuid}/validate")
        if g == "server" and act == "resources":
            return client.get(f"/servers/{args.uuid}/resources")
    if g == "deployment":
        if args.action == "get":
            return client.get(f"/deployments/{args.uuid}")
        if args.action == "history":
            return client.get(f"/deployments/applications/{args.app_uuid}")
        if args.action == "wait":
            return wait_for_deployment(client, args.uuid, timeout=args.timeout)

    # lifecycle (apps/db/svc) ----------------------------------------------------
    if g in ("apps", "db", "svc") and args.action in ("start", "stop", "restart"):
        return client.post(f"{ITEM_PREFIX[g]}/{args.uuid}/{args.action}")

    # delete (destructive) -------------------------------------------------------
    if g in LIST_PATHS and args.action == "delete":
        name = None
        if client.apply:
            # Verify the real resource name so --confirm can't be bypassed.
            # Let an APIError from the lookup propagate (don't fall back to a value
            # that makes the confirm check trivially pass).
            item = client.get(f"{ITEM_PREFIX[g]}/{args.uuid}")
            name = item.get("name")
            if name is None:
                raise ConfirmError(
                    f"Could not determine the name of {g} {args.uuid} to verify "
                    f"--confirm; refusing to delete.")
        return client.delete(f"{ITEM_PREFIX[g]}/{args.uuid}",
                             destructive=True, confirm=args.confirm, resource_name=name)

    # create ---------------------------------------------------------------------
    if g == "apps" and args.action == "create":
        return client.post(APP_CREATE[args.variant], body=_read_json_arg(args.json_body))
    if g == "db" and args.action == "create":
        return client.post(DB_ENGINES[args.engine], body=_read_json_arg(args.json_body))

    # env ------------------------------------------------------------------------
    if g == "env":
        base = f"{ENV_BASE[args.target]}/{args.uuid}/envs"
        if args.action == "list":
            return client.get(base)
        if args.action == "set":
            k, _, v = args.pair.partition("=")
            return client.post(base, body={"key": k, "value": v})
        if args.action == "set-bulk":
            data = parse_env_file(Path(args.file).read_text(encoding="utf-8"))
            body = {"data": [{"key": k, "value": v} for k, v in data.items()]}
            return client.patch(base + "/bulk", body=body)
        if args.action == "delete":
            return client.delete(f"{base}/{args.env_uuid}",
                                 destructive=True, confirm=args.confirm,
                                 resource_name=args.env_uuid)

    # deploy ---------------------------------------------------------------------
    if g == "deploy":
        params = {}
        if args.uuid:
            params["uuid"] = args.uuid
        if args.tag:
            params["tag"] = args.tag
        if args.force:
            params["force"] = "true"
        return client.post("/deploy?" + urllib.parse.urlencode(params))

    raise ValueError(f"unknown or write command not yet wired: {g} {getattr(args,'action',None)}")


# ── output + entrypoint ───────────────────────────────────────────────────────
def format_output(obj, as_json=False):
    if isinstance(obj, DryRun):
        return json.dumps(obj.to_dict(), indent=2) if as_json else str(obj)
    if as_json:
        return json.dumps(obj, indent=2)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "uuid" in obj[0]:
        rows = [f"{i.get('uuid',''):36}  {str(i.get('name','')):24}  {i.get('status','')}"
                for i in obj]
        return "\n".join(rows)
    return json.dumps(obj, indent=2)


def run_self_test(pattern=None):
    import unittest
    here = Path(__file__).resolve().parent
    loader = unittest.TestLoader()
    if pattern:
        loader.testNamePatterns = [f"*{pattern}*"]
    suite = loader.discover(str(here), pattern="test_coolify.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    if not args.group:
        parser.print_help()
        return 2
    try:
        base, token = load_config()
        client = CoolifyClient(base, token, apply=args.apply)
        out = dispatch(client, args)
        print(format_output(out, as_json=args.json))
        return 0
    except (ConfigError, ConfirmError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except APIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
