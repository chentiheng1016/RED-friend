"""tests 套件初始化。

唯一目的：在 unittest 匯入任何測試模組（進而 import agent_core）之前，把 runtime
根 RED_RUNTIME_DIR 導向一次性 tmp dir。否則在主 checkout 直接跑測試時，run_history
等執行記錄器會寫進 live var/runs/，假 run（如故意 raise 的 send_gmail）混入
dashboard_alerts「今日任務失敗率」→ 誤報告警（見 2026-07-11 事故）。

為何放這裡而非 conftest.py：本 repo 跑 `unittest discover`，conftest 的 pytest
autouse 不生效（見 CLAUDE.md）。package __init__ 在 discover 匯入 tests.* 時必跑，
一處涵蓋 `make test` / 手動單測（`python -m unittest tests.test_x`）/ CI 全部入口。
已顯式設 RED_RUNTIME_DIR 者（如 test_deploy_lock 起子程序）不覆蓋。

🚨 這層隔離有個致命前提：**agent_core 還沒被 import**。`RUNTIME_ROOT` 是
`logging_and_paths` 在 import time 從 env 算好的常數，晚一步設 env 完全無效。而
`unittest discover -s tests`（**少了 `-t .`**）會把 tests/ 當 top_level_dir、測試
模組以 top-level 名匯入 —— 本檔於是完全不在載入路徑上（只有某支測試剛好寫
`from tests.x import ...` 時才被順帶拖進來，那時早就來不及了）。

所以「重導失敗」這件事**不能靠本檔攔**：在這裡 raise 會被 unittest 包成一筆
`_FailedTest` 然後照跑完全套。真正的守門放在做出決定的那一行旁邊，見
`agent_core/logging_and_paths._guard_tests_never_touch_live_runtime()`；那裡是唯一
必經之路，會直接 os._exit 停掉整個 process。踩到的後果與實測污染數字也記在那。

官方入口全都安全（CI 與 make test/test-quiet 用 `-t .`、pre-commit smoke 與手動
單測用 `tests.test_x` package 形式），會踩到的只有裸 discover。
"""
import atexit
import os
import shutil
import socket
import tempfile

if not os.environ.get("RED_RUNTIME_DIR"):
    _runtime_dir = tempfile.mkdtemp(prefix="red-test-runtime-")
    os.environ["RED_RUNTIME_DIR"] = _runtime_dir
    atexit.register(shutil.rmtree, _runtime_dir, ignore_errors=True)


# ── 對外連線防護網（同上：放這裡而非 conftest，理由見上面的 docstring）─────────
# 2026-08-14 用 socket 層普查掃全套 5,269 筆，抓到 1 筆真的在打 api.telegram.org：
# test_tg_send_rejects_ok_false_payload_even_with_http_200 patch 錯 seam（真 requests
# 有 Session，tg_send 走的是共用 Session 物件），每跑一次測試就對 Telegram 連 24 次，
# 斷言還是靠「真的失敗」假通過的。同一類問題另有 2026-08-12 的
# test_station_capacity_chart 真連 Drive（#391）——都是「有憑證/有網路的機器才紅」。
#
# 這裡一律擋掉外部位址，讓漏 mock 不再變成靜默的對外流量。localhost 與 unix socket
# 放行（in-process 測試伺服器、tool_rpc 的 .sock）。
# ⚠️ 擋下來會以 OSError 呈現，被 broad-except 接住的呼叫端仍可能默默降級成綠 —— 想
# 主動普查「誰在摸外部服務」要另外記帳（見 #391 那支測試的 setUpModule 手法）。
# 真的需要對外（本機手動跑整合測試）時設 RED_TESTS_ALLOW_NETWORK=1。
if os.environ.get("RED_TESTS_ALLOW_NETWORK") != "1":
    _LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex

    def _test_net_host(address):
        if isinstance(address, tuple) and address:
            return str(address[0])
        return str(address)

    def _test_net_is_local(address):
        host = _test_net_host(address)
        return host in _LOCAL_HOSTS or host.startswith("/")

    def _test_net_blocked(address):
        return OSError(
            f"測試不准對外連線：{_test_net_host(address)}"
            "（漏 mock？真要放行設 RED_TESTS_ALLOW_NETWORK=1）")

    def _guarded_connect(self, address):
        if not _test_net_is_local(address):
            raise _test_net_blocked(address)
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if not _test_net_is_local(address):
            raise _test_net_blocked(address)
        return _real_connect_ex(self, address)

    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
