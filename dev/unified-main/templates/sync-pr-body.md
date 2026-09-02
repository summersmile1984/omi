## Upstream sync

- Upstream range: `<old-sha>..<new-sha>` (`N` commits, from `YYYY-MM-DD` to `YYYY-MM-DD`)
- Real conflicts (`git merge-tree --write-tree main upstream/main | grep -c '^CONFLICT'`): `N`

### Conflict dispositions

| File | Class (bot / injection point / client seam / brand hook / doc) | Disposition (theirs / ours / manual) | Follow-up to remove the conflict source |
|---|---|---|---|
| | | | |

### Verification

- [ ] `python3 scripts/brand/apply.py --brand <brand> --check-clean` → zero diff
- [ ] `python3 scripts/brand/check.py --brand <brand>` → 0 leaks on all surfaces
- [ ] `make preflight` → green (paste summary)
- [ ] Contract suite vs self-host (`deploy/self-host` compose) → pass
- [ ] Contract suite vs Cloudflare (`wrangler dev`) → pass
- [ ] Fork-owned tests discovered and green (`backend/tests/unit/fork/`, `app/test/fork/`, desktop `Fork*` tests)

### Notes for `dev/unified-main/sync-log.md`

`YYYY-MM-DD | upstream <new-sha> | conflicts N | minutes M | <notes>`
