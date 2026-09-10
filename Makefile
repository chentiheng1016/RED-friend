PYTHON := .venv/bin/python
PIP := .venv/bin/pip

MAIN_REQ_FILES := requirements-core.txt requirements-rag.txt requirements-web.txt requirements-docs.txt requirements-dev.txt

.PHONY: help install install-full install-dev install-hooks migrate-runtime housekeeping test test-quiet lint dependency-audit secrets-scan security-audit node-audit node-test node-launch-smoke operational-db-smoke red-smoke red-smoke-local clean-runtime

help:
	@echo "Targets:"
	@echo "  make install       # install lean default bundles into .venv"
	@echo "  make install-full  # install every optional bundle"
	@echo "  make install-dev   # install runtime + dev dependencies"
	@echo "  make install-hooks # install pre-commit + post-merge hooks"
	@echo "  make migrate-runtime # move legacy runtime files into var/"
	@echo "  make housekeeping  # prune old var/logs, runs, workflows"
	@echo "  make test          # run unittest suite"
	@echo "  make test-quiet    # run unittest suite with compact output"
	@echo "  make lint          # run ruff"
	@echo "  make dependency-audit # audit app dependencies"
	@echo "  make security-audit # run Python dependency + secrets checks"
	@echo "  make node-audit    # npm audit with scoped advisory exemptions"
	@echo "  make node-test     # run Node/Puppeteer import smoke"
	@echo "  make node-launch-smoke # launch local Chrome through Puppeteer"
	@echo "  make operational-db-smoke # run disposable Postgres schema smoke"
	@echo "  make red-smoke     # run full post-deploy smoke"
	@echo "  make red-smoke-local # run smoke without Google/Telegram side effects"
	@echo "  make clean-runtime # remove generated runtime caches/logs"

install:
	$(PIP) install -r requirements-core.txt -r requirements-rag.txt -r requirements-docs.txt

install-full:
	$(PIP) install -r requirements.txt

install-dev: install
	$(PIP) install -r requirements-dev.txt

install-hooks:
	# pre-merge-commit stage 也要裝：merge 自動產生的 commit 不走 pre-commit hook，
	# 只走 pre-merge-commit —— 少裝它，merge origin/main 撞出的重複定義就沒人攔。
	$(PYTHON) -m pre_commit install --hook-type pre-commit --hook-type pre-merge-commit
	bash scripts/install-hooks.sh

migrate-runtime:
	$(PYTHON) scripts/migrate_runtime_to_var.py --apply

housekeeping:
	$(PYTHON) launchd/scripts/housekeeping.py

# `-t .` 讓 tests 以 package 匯入 → 執行 tests/__init__.py（把 RED_RUNTIME_DIR 導向
# 一次性 tmp dir，測試才不會把假 run 寫進 live var/runs/ → dashboard「任務失敗率」誤報）。
# 拿掉 -t . 會回退成 top-level 匯入、__init__ 不跑、隔離失效。見 tests/__init__.py。
test:
	AGENT_DAEMON_MODE=1 $(PYTHON) -m unittest discover -s tests -t .

test-quiet:
	AGENT_DAEMON_MODE=1 $(PYTHON) -m unittest discover -s tests -t . -q

lint:
	git ls-files -z -- '*.py' | xargs -0 $(PYTHON) -m ruff check

# chromadb server-side advisories. NONE has a fixed release upstream (chromadb
# 1.5.9 is the latest published version as of 2026-08-25), and every one of them
# requires reaching the Chroma HTTP server, which binds 127.0.0.1 only (launchd
# com.xiaohong.chroma --host 127.0.0.1 --port 8000; verified with lsof: the
# listener is 127.0.0.1:8000, not 0.0.0.0). Not network-reachable ⇒ scoped
# suppression so a fixless upstream CVE cannot mask real test signal.
#
#   CVE-2026-45829  pre-auth RCE in the FastAPI server (PYSEC-2026-311 is the
#                   same advisory under its PyPI id — covered by this entry).
#   CVE-2026-45830  missing authz validation: any authenticated user can
#                   read/write/delete data in any tenant's collections.
#   CVE-2026-45833  code injection via a malicious model repository (authed).
#   CVE-2026-45831  SimpleRBACAuthorizationProvider never checks which tenant/db
#                   a permission applies to.
#
# ⚠️ Remove these the moment a patched chromadb ships and requirements-rag.txt is
# bumped — and re-check the binding assumption if the Chroma server is ever
# exposed beyond loopback (e.g. Cloud Run role), because that is the only thing
# keeping these out of reach.
# tests/test_chromadb_cve_ignore_scope.py 守著這份清單：pin 一動就紅（逼你在升版
# 當下回來確認哪幾條可以刪），且每個被壓掉的 CVE 都必須在上面有交代。
CHROMADB_CVE_IGNORE := --ignore-vuln CVE-2026-45829 \
                       --ignore-vuln CVE-2026-45830 \
                       --ignore-vuln CVE-2026-45831 \
                       --ignore-vuln CVE-2026-45833

dependency-audit:
	.venv/bin/pip-audit $(foreach req,$(MAIN_REQ_FILES),-r $(req)) --progress-spinner off $(CHROMADB_CVE_IGNORE)

secrets-scan:
	.venv/bin/detect-secrets scan --baseline .secrets.baseline

security-audit: secrets-scan dependency-audit

# 目前無豁免。歷史：GHSA-mh99-v99m-4gvg (CVE-2026-14257) brace-expansion DoS 曾
# 因「只有 5.0.8 修、而它的 named-export API 會拆掉 puppeteer-extra-plugin-stealth
# 釘住的 minimatch@3 → glob@7 → rimraf@3 鏈」而 scoped 豁免（2026-07-25）。
# 2026-08-04 上游補了 1.1.18（1.x 修補線，API 不變）並同時修掉繞過原緩解的
# GHSA-rgw5-rvv9-x895 → lockfile 升上去、豁免一併拿掉（留著會遮住之後的回歸）。
NODE_AUDIT_IGNORE :=

node-audit:
	node scripts/node/npm_audit_gate.cjs --audit-level=moderate $(NODE_AUDIT_IGNORE)

node-test:
	npm test

node-launch-smoke:
	npm run smoke:node:launch

operational-db-smoke:
	$(PYTHON) scripts/ci_operational_db_smoke.py --dry-run-limit 100

red-smoke:
	./bin/red-smoke

red-smoke-local:
	./bin/red-smoke --skip-telegram --skip-google

clean-runtime:
	rm -rf .pytest_cache .ruff_cache __pycache__ agent_core/__pycache__ tests/__pycache__
