#!/usr/bin/env bash
# End-to-end check against a running `docker compose up` stack.
set -euo pipefail
BASE=${BASE:-http://localhost:8000}
for i in $(seq 1 60); do curl -fs "$BASE/health" >/dev/null && break || sleep 3; done
curl -fs "$BASE/health"; echo
TOKEN=$(curl -fs -X POST "$BASE/auth/token" -H 'content-type: application/json' \
  -d '{"username":"manager","password":"manager-demo"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"
RUN=$(curl -fs -X POST "$BASE/agents/run" -H "$AUTH" -H 'content-type: application/json' \
  -d '{"request":"Find delayed orders, check the refund policy, create tickets and email customers"}')
echo "$RUN" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert d["status"]=="awaiting_approval",d;print("paused for approval with",len(d["proposals"]),"proposals")'
RID=$(echo "$RUN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
DONE=$(curl -fs -X POST "$BASE/runs/$RID/approve" -H "$AUTH" -H 'content-type: application/json' -d '{"approve":true}')
echo "$DONE" | python3 -c 'import sys,json;d=json.load(sys.stdin);r=d["report"];assert d["status"]=="completed",d;assert r["tickets_created"]>=1,r;print("completed:",r["tickets_created"],"tickets,",r["notifications_sent"],"notifications")'
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/runs/$RID/approve" -H "$AUTH" -H 'content-type: application/json' -d '{"approve":true}')
test "$CODE" = "409" && echo "double approval correctly rejected (409)"
echo "SMOKE TEST PASSED"
