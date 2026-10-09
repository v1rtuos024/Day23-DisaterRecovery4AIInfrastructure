# Region A down — operator runbook

Run from the repository root with the Python environment activated. Primary A:
`:8001`; standby B: `:8002`; edge: `:8080`. Owner: on-call SRE; decision authority:
incident commander (IC). Use `--backend minio` instead of `fs` if the drill uses MinIO.
A standby may have `/readyz=503` before restore; `/healthz=200` alone does not mean
it can serve inference. Do not seed/reset state or overwrite evidence during an incident.

Before the drill, ensure a snapshot exists: `python state/snapshot.py put --region a --backend fs`.
Start user evidence **before the outage**, in a separate terminal:
`python loadgen/traffic.py --duration 300 --rps 2 --out reports/drill-2-withdr.jsonl`.
Start detection in another terminal:
`python dr/health_checker.py --interval 5 --threshold 3 --duration 300 --out reports/health-events.jsonl`.
Keep both running through recovery. Preserve each drill's logs before starting another.

Execute step 1 once. It runs the whole checklist; commands in rows 2–7 inspect its
results and must not launch another failover. Answer `y` only after the IC authorizes
cutover. Empty input, `n`, or EOF aborts. `--auto` bypasses approval for graded drills/CI only.

| # | Step | Copy-paste command | Success signal | Owner |
|---|---|---|---|---|
| 1 | Confirm outage and launch checklist | `python dr/runbook.py --primary a --target b --backend fs` | Step 1 logs three A `/readyz` failures 5s apart and B observations; approval prompt appears. If A recovers, abort. | On-call SRE |
| 2 | Open incident and start clock | `tail -n 7 reports/runbook-run.jsonl` | Step 2 has incident `ts`, `t_outage` from chaos, and announcement. Incident ts is after outage ts. Without chaos, outage is unknown; do not invent RTO. On-call announces through the team's incident channel. | On-call / IC |
| 3 | Restore and scale once | `tail -n 5 reports/failover-events.jsonl` | Steps 1–3 show verified target, restored `embed_model_version`, `rpo_seconds`, `docs_lost`, then `pool_state=full`. Missing/null RPO means unmeasured data loss, not zero loss. | Platform SRE |
| 4 | Verify replica and warmup | `curl --fail --max-time 3 http://127.0.0.1:8002/readyz` | HTTP 200; step 4 in runbook log has weights=true, vector_count>0 and model version. Failover readiness wait is 60s maximum; timeout aborts before pointer change. | ML / data on-call |
| 5 | Verify cutover | `curl --fail --max-time 3 http://127.0.0.1:8080/edge/state` | `active_region=b`; failover step `5_dns_cutover` and runbook step 5 are successful. Never manually change the pointer before readiness. | Platform SRE, IC authorizes |
| 6 | Verify golden signals | `tail -n 2 reports/runbook-run.jsonl` | Step 6 contains 10 real B inference requests, correct served_by, error_rate=0 and p95_latency_ms<1000. These are lab acceptance limits; all 10 latencies, including failures, enter p95. Runbook exit 0 means checks passed. | Service on-call |
| 7 | Measure and document | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | valid=true, warnings empty, RTO verdict PASS, measured RPO/docs_lost present. Step 7 logs elapsed_s and the measurement command. Fill reports/rto-evidence.md and reports/postmortem.md with real log path:line references. | Incident scribe / IC |

Logs: `reports/runbook-run.jsonl` holds checklist steps; `reports/failover-events.jsonl`
holds the five cutover substeps. Exit 1 requires investigation. After a failed run,
inspect `failed_step`/`reason` and `edge/active_region` before deciding on a retry.
A failure after cutover can leave B active; no automatic failback occurs. `elapsed_s`
is operator workflow time, not user RTO, which starts at the outage timestamp.

**Rollback / return to A:** The IC alone authorizes the traffic move; platform SRE
executes it, with ML/data on-call approving data/model consistency. Trigger an
assessment if B has any error in the 10-request check, p95 >=1000ms, corrupted or
incompatible state, or sustained user errors after cutover. Do not return traffic
merely because A's process is alive. If A is unsafe, keep the current pointer and
escalate rather than create a second outage.

Recover A's process/network first: `python chaos/kill_region.py restore --region a --backend bare`
(use `--backend docker` for Docker). If bare restore reports `need_manual_start`,
restart only A using the serving command/environment in `scripts/up_bare.sh`; avoid
resetting the whole stack. Require three successful A `/readyz` checks 5s apart:
`for i in 1 2 3; do curl --fail --max-time 3 http://127.0.0.1:8001/readyz || break; sleep 5; done`.

Before copying state, data on-call must choose the authoritative dataset, reconcile
any divergent A/B writes, pause ingestion/replication, and preserve both databases.
If B is authoritative and healthy, publish its snapshot:
`python state/snapshot.py put --region b --backend fs`.
Then, **only with IC approval**, execute:
`python dr/failover.py --target a --backend fs --wait 60`.
This restores B's snapshot into A, verifies readiness and changes the pointer;
using an old A snapshot can lose writes accepted by B. If either restore or readiness
fails, abort and keep B active. Verify `curl --fail --max-time 3 http://127.0.0.1:8080/edge/state`
shows A and run `python loadgen/traffic.py --duration 30 --rps 2 --out reports/failback-check.jsonl`;
require successful requests served by A before the IC closes the incident. Resume
writers against the chosen primary. No automated bidirectional failover or repeated
cutover retries; each traffic move needs a fresh IC decision.
