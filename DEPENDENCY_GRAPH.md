# TRAIDER Task Dependency Graph & Execution Order

## All 41 Tasks with Dependencies

### Legend
- ✓ = Task status indicator
- → = Depends on
- | = Parallel execution possible

---

## Complete Dependency Tree

```
PHASE 1: SETUP
═══════════════

setup-env (1)
    ↓
setup-deps (2)
    ├→ setup-docker (3)
    │     ↓
    │   setup-aws (4)
    │
    └→ config-create (5)
          ↓
      config-validation (6)
          ├→ data-historical (7)
          │     ↓
          │   data-tests (9)
          │
          ├→ data-live (8)
          │     ↓
          │   feature-create (10)
          │     ↓
          │   feature-validation (11)
          │
          ├→ state-db-schema (12)
          │     ↓
          │   state-persistence (13)
          │
          ├→ logging-setup (14)
          │     ↓
          │   alerting-setup (15)
          │
          └→ execution-alpaca (25)

PHASE 2: CORE MODULES (parallel from config-validation)
═════════════════════════════════════════════════════════

[See above - all depend on config-validation]

PHASE 3: STRATEGY & BACKTEST
════════════════════════════

feature-validation (11)
    ├→ backtest-framework (16)
    │   ├→ state-persistence (13)
    │   ├→ logging-setup (14)
    │   └→ backtest-dummy-signals (17)
    │         ↓
    │       [test backtest works]
    │
    └→ model-simple (18)
          ↓
      model-training (19)
          ↓
      backtest-strategy (20)
      [using trained model]
          ↓
      backtest-report (21)   ← later moved to Phase 5; ✅ COMPLETE there
      [validate: Sharpe ≥ 1.0, DD ≤ 25%, WR ≥ 55%]

PHASE 4: RISK & EXECUTION
══════════════════════════

state-persistence (13)
    ├→ risk-position-sizing (22)
    │     ↓
    │   risk-validation (24)
    │
    └→ risk-circuit-breaker (23)
          ↓
      risk-validation (24)

execution-alpaca (25)
    ├→ config-validation (6)
    ├→ state-persistence (13)
    └→ execution-retry (26)
          ├→ logging-setup (14)
          └→ [ready for scheduler]

PHASE 5: DEFERRED — CORE & BACKTEST COMPLETION
══════════════════════════════════════════════

(Nothing in Phase 4 depends on these except state-persistence, which Phase 4
stubs with simple in-memory state until task 13 lands.)

config-validation (6: remaining Alpaca connectivity check)
      ↓
state-db-schema (12) → state-persistence (13)
logging-setup (14) → alerting-setup (15 remaining: alert feed in portal)

model-training (19)   [optional — only if the ML path stays in scope]
      ↓
backtest-strategy (20)   [GATE CHECK: Sharpe ≥ 1.0, DD ≤ 25%, WR ≥ 55%]
      ↓
backtest-report (21) ✅ DONE — standalone /report page; reads runs/<run_id>.json in full
      ↓
parameter-sensitivity sweep (split out of 21) ⏸️ OPEN — no dependency for Phase 6
      ↓
[ready for Phase 6 Integration]

PHASE 6: INTEGRATION
════════════════════

data-live (8)
    ├→ scheduler-create (27)
    │
model-simple (18)
    ├→ scheduler-create (27)
    │
risk-validation (24)
    ├→ scheduler-create (27)
    │
execution-retry (26)
    ├→ scheduler-create (27)
    │
state-persistence (13)
    ├→ scheduler-create (27)
    │     ↓
    │ scheduler-error-handling (28)
    │     ↓
    │ main-entry (29)
    │     ├→ alerting-setup (15)
    │     └→ [ready for testing]

PHASE 7: TESTING
════════════════

backtest-report (21) ✅ done (standalone /report page, reads runs/ in full)
    ├→ test-unit (30)
    │
main-entry (29)
    ├→ test-unit (30)
    │     ↓
    │ test-integration (31)
    │     ├→ test-paper-trading (32)
    │     └→ test-load (33)
    │           ↓
    │       [paper trading can run in parallel]

PHASE 8: DEPLOYMENT
════════════════════

test-load (33)
    ↓
deploy-docker-build (34)
    ├→ [verify locally]
    └→ deploy-aws-setup (35)
        ├→ deploy-aws-logging (36)
        ├→ deploy-monitoring (37)
        │   └→ live-go-live (39)
        │
        └→ deploy-backup (38)
             └→ live-go-live (39)

PHASE 9: LIVE TRADING
══════════════════════

live-go-live (39)
    ├→ deploy-monitoring (37) ✓
    ├→ deploy-backup (38) ✓
    └→ live-monitoring (40)
         └→ live-iteration (41)
```

---

## Critical Path (Longest Dependency Chain)

```
1. setup-env
2. setup-deps
3. config-create → config-validation
4. data-historical → feature-create → feature-validation
5. backtest-framework → model-simple
6. risk-position-sizing → risk-circuit-breaker → risk-validation → execution-alpaca → execution-retry   (Phase 4)
7. state-persistence → logging-setup → backtest-strategy → backtest-report   (Phase 5; report ✅ done)
8. scheduler-create → scheduler-error-handling → main-entry                  (Phase 6)
9. test-unit → test-integration → test-paper-trading → test-load             (Phase 7)
10. deploy-docker-build → deploy-aws-setup → deploy-monitoring               (Phase 8)
11. live-go-live                                                             (Phase 9)

CRITICAL PATH LENGTH: ~30 days (longest single chain)
TOTAL PROJECT: ~35 days + 14 days paper trading (concurrent)
```

---

## Parallelization Map

### Can Run in Parallel (Same Time)

**Group A (After config-validation):**
- data-historical (7)
- data-live (8)
- state-db-schema (12)   → now Phase 5
- logging-setup (14)     → now Phase 5
- execution-alpaca (25)
→ All independent, can start same day

**Group B (While backtesting):**
- risk-position-sizing (22)
- risk-circuit-breaker (23)
→ Don't depend on backtest results

**Group C (While coding):**
- Paper trading setup (can prepare the Alpaca paper account while Phase 4-5 run)
- Confirm OpenBB provider + AAPL data availability early (before data-historical)
- Docker/AWS docs (can read while coding)

**Group D (End of Phase 7):**
- Docker build (34)
- Paper trading (32)
→ Paper trading doesn't block Docker build

---

## Recommended Daily Work Schedule

### Week 1
```
Monday (Day 1):    setup-env, setup-deps
Tuesday (Day 2):   setup-docker, setup-aws
Wednesday (Day 3): config-create, config-validation
Thursday (Day 4):  data-historical, data-live   (state-db-schema deferred → Phase 5)
Friday (Day 5):    data-tests                   (state-persistence, logging-setup deferred → Phase 5)
```

### Week 2
```
Monday (Day 6):    feature-create               (alerting-setup deferred → Phase 5)
Tuesday (Day 7):   feature-validation
Wednesday (Day 8): backtest-framework           (backtest-dummy-signals deferred → Phase 5)
Thursday (Day 9):  model-simple                 (model-training deferred → Phase 5)
Friday (Day 10):   Phase 4 starts: risk-position-sizing, risk-circuit-breaker   (backtest-strategy + backtest-report deferred → Phase 5 [GATE CHECK])
```

### Week 3
```
Monday (Day 11):   risk-position-sizing, risk-circuit-breaker
Tuesday (Day 12):  risk-validation (all risk checks complete)
Wednesday (Day 13): execution-alpaca, execution-retry
Thursday (Day 14): scheduler-create, scheduler-error-handling
Friday (Day 15):   main-entry [core system ready]
```

### Week 4
```
Monday (Day 16):   test-unit (40% test coverage min)
Tuesday (Day 17):  test-integration
Wednesday (Day 18): test-unit continued (80% target)
Thursday (Day 19): Paper trading begins (start of 1-2 week run)
Friday (Day 20):   [No new coding, monitoring paper trades]
```

### Week 5
```
Monday (Day 21):   [Paper trading ongoing]
Tuesday (Day 22):  deploy-docker-build (build Docker locally)
Wednesday (Day 23): deploy-aws-setup (Lightsail deployment)
Thursday (Day 24): deploy-aws-logging, deploy-monitoring
Friday (Day 25):   deploy-backup, final checks [GATE CHECK]
```

### Week 6+
```
[Paper trading ends after 1-2 weeks]
Monday (Day 26):   live-go-live (1% position size)
Ongoing:           live-monitoring, live-iteration (monthly retrains)
```

---

## Task Batches for Parallel Work (Multi-Dev)

If you have multiple developers:

### Dev 1: Data & Features
- Weeks 1-2: setup, config, data layer, features
- Weeks 3-4: Tests for above modules
- Weeks 5+: Monitoring

### Dev 2: Strategy & Backtesting  
- Weeks 1-2: Backtest framework
- Weeks 2-3: Model development
- Weeks 3-4: Strategy validation & testing
- Weeks 5+: Model retraining

### Dev 3: Risk & Execution
- Weeks 2-3: Risk module, execution module
- Weeks 3-4: Integration & scheduler tests
- Weeks 5+: Paper trading monitoring

### DevOps: Docker & Infrastructure
- Weeks 1: Initial Dockerfile
- Weeks 4-5: Docker build, AWS deployment
- Weeks 5+: Monitoring, backups

---

## Dependency Matrix (Quick Lookup)

| Task | Depends On | Tasks That Depend On It |
|------|-----------|------------------------|
| setup-env | — | setup-deps |
| setup-deps | setup-env | setup-docker, config-create |
| setup-docker | setup-deps | setup-aws |
| setup-aws | setup-docker | deploy-aws-setup |
| config-create | setup-deps | config-validation |
| config-validation | config-create | data-historical, data-live, state-db-schema, logging-setup, execution-alpaca |
| data-historical | config-validation | data-tests, feature-create, backtest-framework |
| data-live | config-validation | scheduler-create |
| data-tests | data-historical, data-live | — |
| feature-create | data-historical | feature-validation, backtest-framework |
| feature-validation | feature-create | model-simple, backtest-framework, test-unit |
| state-db-schema | config-validation | state-persistence |
| state-persistence | state-db-schema | backtest-framework, risk-position-sizing, risk-circuit-breaker, execution-alpaca, scheduler-create, test-unit |
| logging-setup | config-validation | alerting-setup, backtest-framework, execution-retry, test-unit |
| alerting-setup | logging-setup | main-entry |
| backtest-framework | feature-validation, state-persistence, logging-setup | backtest-dummy-signals, backtest-strategy, test-unit |
| backtest-dummy-signals | backtest-framework | backtest-strategy (validation run) |
| model-simple | feature-validation | model-training, scheduler-create, test-unit |
| model-training | model-simple | backtest-strategy, test-unit |
| backtest-strategy | backtest-framework, model-training | backtest-report, test-unit |
| backtest-report | backtest-strategy | test-unit |
| risk-position-sizing | state-persistence | risk-validation |
| risk-circuit-breaker | state-persistence | risk-validation |
| risk-validation | risk-position-sizing, risk-circuit-breaker | scheduler-create, test-unit |
| execution-alpaca | config-validation, state-persistence | execution-retry |
| execution-retry | execution-alpaca, logging-setup | scheduler-create, test-unit |
| scheduler-create | data-live, model-simple, risk-validation, execution-retry, state-persistence | scheduler-error-handling, test-unit |
| scheduler-error-handling | scheduler-create | main-entry |
| main-entry | scheduler-error-handling, alerting-setup | test-unit, test-integration |
| test-unit | backtest-report, main-entry, feature-validation, model-simple, model-training, backtest-strategy, risk-validation, execution-retry, scheduler-create | test-integration |
| test-integration | test-unit | test-paper-trading, test-load |
| test-paper-trading | test-integration | test-load (can run parallel) |
| test-load | test-integration | deploy-docker-build |
| deploy-docker-build | test-load | deploy-aws-setup |
| deploy-aws-setup | deploy-docker-build | deploy-aws-logging, deploy-backup |
| deploy-aws-logging | deploy-aws-setup | deploy-monitoring |
| deploy-monitoring | deploy-aws-logging | live-go-live |
| deploy-backup | deploy-aws-setup | live-go-live |
| live-go-live | deploy-monitoring, deploy-backup | live-monitoring |
| live-monitoring | live-go-live | live-iteration |
| live-iteration | live-monitoring | — |

---

## No-Block Zones (Can Work Independently)

These have no dependencies and can be done anytime after project setup:

- ✓ Writing README.md
- ✓ Creating .gitignore
- ✓ Setting up GitHub repo
- ✓ Creating architecture diagrams
- ✓ Writing deployment runbooks

---

## Bottleneck Analysis

**Longest individual tasks (may need most time):**
1. `test-paper-trading` (14 days real time - run in parallel with other work)
2. `model-training` (5-7 days if tuning hyperparameters)
3. `backtest-strategy` (3-5 days if iterating on model)
4. `test-integration` (3-4 days, comprehensive)

**Critical dependencies:**
- Everything waits on `config-validation` (must be first)
- Strategy waits on `backtest-report` (validates before proceeding)
- Deployment waits on `test-load` (must pass before going to prod)
- Live waits on paper trading (non-negotiable)

---

## SQL Query: Ready-to-Start Tasks

At any point, find tasks with no pending dependencies:

```sql
SELECT t.id, t.title, COUNT(td.depends_on) as dependency_count
FROM traider_plan t
LEFT JOIN traider_deps td ON t.id = td.task_id 
  AND EXISTS (
    SELECT 1 FROM traider_plan dep 
    WHERE dep.id = td.depends_on AND dep.status != 'done'
  )
WHERE t.status = 'pending'
GROUP BY t.id
HAVING dependency_count = 0
ORDER BY t.order_num;
```

---

## Example: Today's Work

Let's say you're starting now. Today's work:

```bash
# Task 1: setup-env
mkdir -p traider/src/{config,data,features,model,risk,execution,state,scheduler,logging}
mkdir -p traider/{backtest,tests,docker}

# Task 2: setup-deps (requires activated venv)
source .venv/bin/activate
pip install openbb requests schedule pydantic sqlalchemy python-dotenv numpy pandas scikit-learn pytest fastapi uvicorn docker
pip freeze > requirements.txt

# Task 3: setup-docker
# Create docker/Dockerfile with Python + OpenBB (data) + Alpaca (execution)

# Task 4: setup-aws
# Create AWS deployment documentation

# Tasks 5-6: Create config module + validation
# Create src/config/settings.py with Pydantic BaseSettings

Done! Move to next day's tasks.
```

---

## Status Board Template

Print this and fill in daily:

```
Week 1 Status:
[Day 1] ☐ setup-env      ☐ setup-deps
[Day 2] ☐ setup-docker   ☐ setup-aws
[Day 3] ☐ config-create  ☐ config-validation
[Day 4] ☐ data-*         ☐ state-db-schema
[Day 5] ☐ feature-*      ☐ logging-*

Week 2 Status:
[Day 6-10] ☐ backtest-framework  ☐ model-* ☐ backtest-strategy
[Gate Check] Backtest Sharpe ≥ 1.0? ☐ YES ☐ NO

Week 3 Status:
[Day 11-15] ☐ risk-*  ☐ execution-*  ☐ scheduler-*  ☐ main-entry

Week 4 Status:
[Day 16-20] ☐ test-unit  ☐ test-integration  [Paper Trading: ACTIVE]

Week 5 Status:
[Day 21-25] ☐ deploy-docker-build  ☐ deploy-aws-*  [Paper Trading: ONGOING]
[Gate Check] Paper Stable? ☐ YES ☐ NO

Week 6+ Status:
[Day 26+] ☐ live-go-live  ☐ live-monitoring  Position Size: __% Capital
```

---

**Version:** 1.0  
**Last Updated:** 2026-08-20  
**Reference:** TRAIDER_PLAN.md, TIMELINE.md, QUICKSTART.md
