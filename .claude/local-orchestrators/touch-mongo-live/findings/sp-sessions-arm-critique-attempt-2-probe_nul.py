import json, os, sys, tempfile
sys.path.insert(0, '/home/laniakea/Projects/touch')
sys.path.insert(0, '/home/laniakea/Projects/touch/tests')
import test_sessions as T
from aggregator import sessions as sess

NUL = chr(0)

# --- A: history.jsonl `project` with an embedded NUL
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    hp = os.path.join(root, "history.jsonl")
    with open(hp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "x", "project": "/home/" + NUL + "/x",
                             "sessionId": T.FOREIGN_IDS[0], "timestamp": 1}) + "\n")
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("A history NUL: OK", len(s.sessions))
    except Exception as e:
        print("A history NUL: SCAN CRASHED:", type(e).__name__, e)

# --- B: registry entry cwd with an embedded NUL
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    p = os.path.join(root, "sessions", "15934.json")
    d = json.load(open(p))
    d["cwd"] = "/home/" + NUL + "/touch"
    json.dump(d, open(p, "w"))
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("B registry NUL: OK", len(s.sessions))
    except Exception as e:
        print("B registry NUL: SCAN CRASHED:", type(e).__name__, e)

# --- C: extremely long project path
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    hp = os.path.join(root, "history.jsonl")
    with open(hp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "x", "project": "/" + "a" * 9000,
                             "sessionId": T.FOREIGN_IDS[1], "timestamp": 1}) + "\n")
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("C long path: OK", len(s.sessions))
    except Exception as e:
        print("C long path: SCAN CRASHED:", type(e).__name__, e)

# --- D: through the wired mirror seam (no handler at all)
from aggregator import mirror as mr
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    hp = os.path.join(root, "history.jsonl")
    with open(hp, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "x", "project": "/home/" + NUL + "/x",
                             "sessionId": T.FOREIGN_IDS[0], "timestamp": 1}) + "\n")
    os.environ["TOUCH_CLAUDE_ROOT"] = root
    os.environ["TOUCH_PROJECT_CWD"] = T.CWD
    try:
        got = list(mr.iter_rebuild_observations(registry_modules=["sessions"]))
        print("D wired rebuild: OK", len(got))
    except Exception as e:
        print("D wired rebuild: CRASHED:", type(e).__name__, e)
