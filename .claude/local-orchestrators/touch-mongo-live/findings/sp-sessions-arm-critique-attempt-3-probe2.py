import os, sys, tempfile
sys.path.insert(0, "/home/laniakea/Projects/touch")
sys.path.insert(0, "/home/laniakea/Projects/touch/tests")
from aggregator import sessions as sess
from aggregator import mirror as mr
import test_sessions as T

with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp); proc = T.fake_stat(tmp)
    calls = []
    real = sess.scan
    def counted(*a, **k):
        calls.append(1); return real(*a, **k)
    sess.scan = counted
    saved = {k: os.environ.get(k) for k in ("TOUCH_CLAUDE_ROOT","TOUCH_PROJECT_CWD")}
    os.environ["TOUCH_CLAUDE_ROOT"]=root; os.environ["TOUCH_PROJECT_CWD"]=T.CWD
    try:
        wired = list(mr.iter_rebuild_observations(registry_modules=["sessions"]))
    finally:
        sess.scan = real
        for k,v in saved.items():
            os.environ.pop(k,None) if v is None else os.environ.__setitem__(k,v)
    print("full scan() invocations per --rebuild pass:", len(calls), "observations:", len(wired))
