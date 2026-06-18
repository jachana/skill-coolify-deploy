import io, json, os, sys, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coolify  # noqa: E402


class RecordingTransport:
    """Fake transport: returns queued (status, headers, payload) responses and
    records each (method, url, body) call. No network. `payload` is a dict/list
    (JSON-encoded), bytes, or None."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        status, hdrs, payload = self.responses.pop(0)
        if isinstance(payload, (bytes, type(None))):
            raw = payload or b""
        else:
            raw = json.dumps(payload).encode()
        return status, hdrs, raw


class TestReads(unittest.TestCase):
    def test_get_returns_parsed_json(self):
        t = RecordingTransport([(200, {}, [{"uuid": "a1", "name": "web"}])])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t)
        out = c.get("/applications")
        self.assertEqual(out[0]["uuid"], "a1")
        self.assertEqual(t.calls[0][0], "GET")
        self.assertTrue(t.calls[0][1].endswith("/applications"))

    def test_http_error_maps_hint(self):
        t = RecordingTransport([(401, {}, {"message": "unauthorized"})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t)
        with self.assertRaises(coolify.APIError) as cm:
            c.get("/applications")
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("ability", cm.exception.hint.lower())


class TestConfig(unittest.TestCase):
    def setUp(self):
        for k in ("COOLIFY_BASE_URL", "COOLIFY_API_TOKEN"):
            os.environ.pop(k, None)

    def test_missing_raises_configerror(self):
        with self.assertRaises(coolify.ConfigError):
            coolify.load_config(start=Path(self.id()))  # nonexistent dir → no .env

    def test_reads_from_env_and_strips_slash(self):
        os.environ["COOLIFY_BASE_URL"] = "https://ex.test/api/v1/"
        os.environ["COOLIFY_API_TOKEN"] = "1|secret"
        base, token = coolify.load_config(start=Path(self.id()))
        self.assertEqual(base, "https://ex.test/api/v1")
        self.assertEqual(token, "1|secret")

    def test_warns_when_base_missing_api_v1(self):
        os.environ["COOLIFY_BASE_URL"] = "https://ex.test"
        os.environ["COOLIFY_API_TOKEN"] = "1|s"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            base, token = coolify.load_config(start=Path(self.id()))
        self.assertEqual(base, "https://ex.test")
        self.assertIn("/api/v1", err.getvalue())


class TestSafety(unittest.TestCase):
    def test_write_is_dryrun_and_skips_transport(self):
        t = RecordingTransport([])  # empty: any transport call would IndexError
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=False)
        out = c.post("/applications/uuid1/restart")
        self.assertIsInstance(out, coolify.DryRun)
        self.assertEqual(t.calls, [])
        self.assertIn("WOULD POST", str(out))

    def test_apply_performs_write(self):
        t = RecordingTransport([(200, {}, {"ok": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        out = c.post("/applications/uuid1/restart")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(t.calls[0][0], "POST")

    def test_destructive_requires_matching_confirm(self):
        t = RecordingTransport([])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        with self.assertRaises(coolify.ConfirmError):
            c.delete("/applications/uuid1", destructive=True,
                     confirm="wrong", resource_name="web")
        self.assertEqual(t.calls, [])

    def test_destructive_with_correct_confirm_proceeds(self):
        t = RecordingTransport([(200, {}, {"deleted": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        out = c.delete("/applications/uuid1", destructive=True,
                       confirm="web", resource_name="web")
        self.assertEqual(out, {"deleted": True})


class TestRetry(unittest.TestCase):
    def test_429_then_success_sleeps_retry_after(self):
        slept = []
        t = RecordingTransport([
            (429, {"Retry-After": "5"}, {"message": "slow down"}),
            (200, {}, {"ok": True}),
        ])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s",
                                  transport=t, sleeper=slept.append)
        out = c.get("/deployments/dep1")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(slept, [5.0])
        self.assertEqual(len(t.calls), 2)

    def test_429_exhausts_then_raises(self):
        t = RecordingTransport([(429, {}, {"m": "x"})] * 10)
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s",
                                  transport=t, sleeper=lambda *_: None, max_retries=3)
        with self.assertRaises(coolify.APIError) as cm:
            c.get("/deployments/dep1")
        self.assertEqual(cm.exception.status, 429)


class TestDeployState(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(coolify.classify_status("finished"), "success")
        self.assertEqual(coolify.classify_status("failed"), "failure")
        self.assertEqual(coolify.classify_status("cancelled-by-user"), "failure")
        self.assertEqual(coolify.classify_status("in_progress"), "running")
        self.assertEqual(coolify.classify_status("queued"), "running")
        self.assertEqual(coolify.classify_status("weird-new-status"), "running")

    def test_wait_polls_until_terminal(self):
        t = RecordingTransport([
            (200, {}, {"status": "queued"}),
            (200, {}, {"status": "in_progress"}),
            (200, {}, {"status": "finished", "logs": "done"}),
        ])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, sleeper=lambda *_: None)
        dep = coolify.wait_for_deployment(c, "dep1", interval=1, sleeper=lambda *_: None)
        self.assertEqual(dep["status"], "finished")
        self.assertEqual(len(t.calls), 3)

    def test_wait_times_out(self):
        t = RecordingTransport([(200, {}, {"status": "in_progress"})] * 50)
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t)
        dep = coolify.wait_for_deployment(c, "dep1", timeout=3, interval=1,
                                          sleeper=lambda *_: None)
        self.assertTrue(dep.get("_timed_out"))


class TestRouting(unittest.TestCase):
    def test_tables_present(self):
        self.assertEqual(coolify.LIST_PATHS["apps"], "/applications")
        self.assertEqual(coolify.APP_CREATE["public"], "/applications/public")
        self.assertEqual(set(coolify.DB_ENGINES), {
            "postgresql", "mysql", "mariadb", "mongodb",
            "redis", "keydb", "dragonfly", "clickhouse"})
        self.assertEqual(coolify.ENV_BASE["svc"], "/services")

    def test_parse_env_file_keyval(self):
        self.assertEqual(
            coolify.parse_env_file("# c\nFOO=bar\n\nBAZ=qux=1\n"),
            {"FOO": "bar", "BAZ": "qux=1"})

    def test_parse_env_file_json(self):
        self.assertEqual(coolify.parse_env_file('{"A":"1","B":"2"}'), {"A": "1", "B": "2"})


class TestDispatchReads(unittest.TestCase):
    def _run(self, argv, responses):
        t = RecordingTransport(responses)
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t)
        args = coolify.build_parser().parse_args(argv)
        return coolify.dispatch(c, args), t

    def test_ping(self):
        out, t = self._run(["ping"], [(200, {}, {"version": "v4.0.0"})])
        self.assertEqual(out["version"], "v4.0.0")
        self.assertTrue(t.calls[0][1].endswith("/version"))

    def test_apps_list(self):
        out, t = self._run(["apps", "list"], [(200, {}, [{"uuid": "a1"}])])
        self.assertEqual(out[0]["uuid"], "a1")
        self.assertTrue(t.calls[0][1].endswith("/applications"))

    def test_db_get(self):
        out, t = self._run(["db", "get", "u9"], [(200, {}, {"uuid": "u9"})])
        self.assertTrue(t.calls[0][1].endswith("/databases/u9"))

    def test_deployment_get(self):
        out, t = self._run(["deployment", "get", "d1"], [(200, {}, {"status": "finished"})])
        self.assertTrue(t.calls[0][1].endswith("/deployments/d1"))


class TestDispatchWrites(unittest.TestCase):
    def _parse(self, argv):
        return coolify.build_parser().parse_args(argv)

    def test_restart_is_dryrun_by_default(self):
        t = RecordingTransport([])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t)
        out = coolify.dispatch(c, self._parse(["apps", "restart", "u1"]))
        self.assertIsInstance(out, coolify.DryRun)
        self.assertEqual(t.calls, [])

    def test_restart_apply_posts(self):
        t = RecordingTransport([(200, {}, {"ok": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        coolify.dispatch(c, self._parse(["--apply", "apps", "restart", "u1"]))
        self.assertEqual(t.calls[0][0], "POST")
        self.assertTrue(t.calls[0][1].endswith("/applications/u1/restart"))

    def test_delete_needs_confirm(self):
        t = RecordingTransport([(200, {}, {"uuid": "u1", "name": "web"})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        args = self._parse(["--apply", "--confirm", "wrong", "apps", "delete", "u1"])
        with self.assertRaises(coolify.ConfirmError):
            coolify.dispatch(c, args)

    def test_env_set_builds_body(self):
        t = RecordingTransport([(200, {}, {"ok": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        coolify.dispatch(c, self._parse(["--apply", "env", "set", "u1", "FOO=bar"]))
        method, url, body = t.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/applications/u1/envs"))
        self.assertEqual(json.loads(body), {"key": "FOO", "value": "bar"})

    def test_deploy_builds_query(self):
        t = RecordingTransport([(200, {}, {"deployments": [{"deployment_uuid": "d1"}]})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        coolify.dispatch(c, self._parse(["--apply", "deploy", "u1", "--force"]))
        url = t.calls[0][1]
        self.assertIn("uuid=u1", url)
        self.assertIn("force=true", url)

    def test_env_delete_needs_matching_confirm(self):
        t = RecordingTransport([])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        args = self._parse(["--apply", "--confirm", "wrong", "env", "delete", "u1", "e9"])
        with self.assertRaises(coolify.ConfirmError):
            coolify.dispatch(c, args)
        self.assertEqual(t.calls, [])

    def test_env_delete_with_matching_confirm_proceeds(self):
        t = RecordingTransport([(200, {}, {"deleted": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        args = self._parse(["--apply", "--confirm", "e9", "env", "delete", "u1", "e9"])
        out = coolify.dispatch(c, args)
        self.assertEqual(out, {"deleted": True})
        self.assertEqual(t.calls[0][0], "DELETE")
        self.assertTrue(t.calls[0][1].endswith("/applications/u1/envs/e9"))

    def test_env_set_bulk_patches_bulk_endpoint(self):
        import tempfile, os as _os
        t = RecordingTransport([(200, {}, {"ok": True})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        fd, path = tempfile.mkstemp(suffix=".env")
        try:
            with _os.fdopen(fd, "w") as fh:
                fh.write("FOO=bar\nBAZ=qux\n")
            coolify.dispatch(c, self._parse(["--apply", "env", "set-bulk", "u1", path]))
        finally:
            _os.remove(path)
        method, url, body = t.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/applications/u1/envs/bulk"))
        self.assertEqual(json.loads(body), {"data": [{"key": "FOO", "value": "bar"},
                                                     {"key": "BAZ", "value": "qux"}]})

    def test_delete_aborts_when_name_lookup_fails(self):
        # name-lookup GET returns 403; the DELETE must never be reached
        t = RecordingTransport([(403, {}, {"message": "forbidden"})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        args = self._parse(["--apply", "--confirm", "web", "apps", "delete", "u1"])
        with self.assertRaises(coolify.APIError):
            coolify.dispatch(c, args)
        self.assertEqual(len(t.calls), 1)          # only the GET happened
        self.assertEqual(t.calls[0][0], "GET")

    def test_delete_aborts_when_name_missing(self):
        # item has no "name" field -> cannot verify --confirm -> refuse
        t = RecordingTransport([(200, {}, {"uuid": "u1"})])
        c = coolify.CoolifyClient("https://ex.test/api/v1", "1|s", transport=t, apply=True)
        args = self._parse(["--apply", "--confirm", "web", "apps", "delete", "u1"])
        with self.assertRaises(coolify.ConfirmError):
            coolify.dispatch(c, args)
        self.assertEqual(len(t.calls), 1)          # GET only, no DELETE


class TestMain(unittest.TestCase):
    def test_format_json_dryrun(self):
        dr = coolify.DryRun("POST", "/applications/u1/restart", None)
        s = coolify.format_output(dr, as_json=True)
        self.assertEqual(json.loads(s)["dry_run"], True)

    def test_format_table_list(self):
        s = coolify.format_output(
            [{"uuid": "u1", "name": "web", "status": "running:healthy"}], as_json=False)
        self.assertIn("u1", s)
        self.assertIn("web", s)
        self.assertIn("running:healthy", s)

    def test_main_selftest_returns_int(self):
        # Run a trivially-true subset so we don't recurse the whole suite slowly:
        rc = coolify.run_self_test(pattern="test_classify")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
