import json, os, sys, tempfile, shutil
sys.path.insert(0, "/home/laniakea/Projects/touch")
sys.path.insert(0, "/home/laniakea/Projects/touch/tests")
from aggregator import sessions as sess
import test_sessions as T

# Probe A: NUL in history `project` -> is anything counted?
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    h = os.path.join(root, "history.jsonl")
    old = open(h).read()
    with open(h, "w") as fh:
        fh.write(json.dumps({"display":"x","project":T.CWD+"\x00","sessionId":T.SEVENTH,"timestamp":1}) + "\n" + old)
    s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
    print("A history-NUL skipped:", {k:v for k,v in s.skipped.items() if v})

# Probe B: unreadable registry dir -> counted?
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    os.chmod(os.path.join(root, "sessions"), 0o000)
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("B registry-dir-unreadable skipped:", {k:v for k,v in s.skipped.items() if v},
              "live docs:", [o.key() for o in s.sessions if o.live])
    finally:
        os.chmod(os.path.join(root, "sessions"), 0o755)

# Probe C: unreadable slug dir -> counted?
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    os.chmod(os.path.join(root, "projects", T.SLUG), 0o000)
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("C slug-dir-unreadable sessions:", len(s.sessions), "skipped:", {k:v for k,v in s.skipped.items() if v})
    finally:
        os.chmod(os.path.join(root, "projects", T.SLUG), 0o755)

# Probe D: history.jsonl unreadable -> counted?
with tempfile.TemporaryDirectory() as tmp:
    root = T.build_root(tmp)
    proc = T.fake_stat(tmp)
    os.chmod(os.path.join(root, "history.jsonl"), 0o000)
    try:
        s = sess.scan(cwd=T.CWD, root=root, proc_root=proc)
        print("D history-unreadable skipped:", {k:v for k,v in s.skipped.items() if v})
    finally:
        os.chmod(os.path.join(root, "history.jsonl"), 0o644)
