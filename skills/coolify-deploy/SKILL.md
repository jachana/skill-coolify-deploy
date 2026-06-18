---
name: coolify-deploy
description: >-
  Trigger a Coolify deployment and VALIDATE it — poll the build to a terminal state,
  then check app health, and report PASS/FAIL with logs on failure. Never reports success
  on a red build. Use when the user says "/coolify-deploy", "deploy on coolify", "redeploy
  my app", "is the coolify build passing", "validate the coolify deploy", "despliega en
  coolify", "vuelve a desplegar", "valida el build de coolify", "¿pasó el build en coolify?".
  Requires scripts/coolify.py + configured creds (token needs the `deploy` ability).
---

# Coolify deploy + build validation

Reply in the user's language (EN/ES). `CO = scripts/coolify.py`.

## 1. Identify the app
- `python $CO apps list` → find the target app uuid (confirm with the user if ambiguous).

## 2. Trigger the deploy (a write → needs --apply)
- Preview: `python $CO deploy <uuid> [--force]` (shows `WOULD POST /deploy?uuid=…`).
- Perform: `python $CO deploy <uuid> [--force] --apply` → capture `deployment_uuid` from the
  response (`deployments[].deployment_uuid`). `--force` = rebuild without cache.

## 3. Validate the build  ⟵ this is the point of the skill
- `python $CO deployment wait <deployment_uuid> --timeout 900`
  - Polls until status is terminal. `finished` = build success; `failed` /
    `cancelled-by-user` = failure. A timeout returns `_timed_out: true`.
- `finished` is NOT proof the app is healthy. Then:
  `python $CO apps get <uuid>` and check `status` is `running:healthy`
  (if the app has `health_check_enabled`).

## 4. Report PASS/FAIL — with evidence (pairs with quality/prove-it-validation)
- **PASS** only if the deployment is `finished` AND the app is healthy. State both.
- **FAIL** if status is `failed`/`cancelled-by-user`/timed-out, OR the app is unhealthy.
  Show the failing build logs: `python $CO deployment get <deployment_uuid>` (the `logs`
  field). Do NOT call the deploy successful.

## Rules
- ⚠ Never report "deployed ✅" on a non-`finished` status or an unhealthy app — show the proof.
- ⚠ One in-flight deploy at a time; if `/deploy` returns 429 the queue is saturated — back off and retry.
- ✅ Resource CRUD (env vars, create/delete) lives in the `coolify` skill.
