"""v6.12.0 — concurrent updates: one user's download must not block others.

Root cause: python-telegram-bot processes updates strictly one-by-one
unless ApplicationBuilder.concurrent_updates() is set (per PTB docs:
"If not called, updates will be processed one by one"). So user A's
slow download queued user B's updates behind it — and even A's own
❌ cancel button couldn't be processed until the download returned.

Fix: .concurrent_updates(True) on the builder. Audit notes:
- no ConversationHandler in the codebase (PTB's concurrency warning N/A)
- no global locks; all shared dicts keyed by uid/token
- store.py writes are atomic (tmp+rename) and fully sync (no await inside)
- blocking download loops run via asyncio.to_thread, never on the loop
- each download gets its own TemporaryDirectory
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOT_SRC = open("bot.py", encoding="utf-8").read()
TREE = ast.parse(BOT_SRC)


def find_builder_chain():
    """Return the Call node for Application.builder()...build()."""
    for node in ast.walk(TREE):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "build"):
            # walk the chained calls back to Application.builder()
            chain = []
            cur = node.func.value
            while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
                if cur.func.attr == "builder":
                    return node, chain  # reached Application.builder()
                chain.append(cur.func.attr)
                cur = cur.func.value
    return None, []


def test_concurrent_updates_enabled():
    node, chain = find_builder_chain()
    assert node is not None, "Application.builder()...build() chain not found"
    assert "concurrent_updates" in chain, (
        f"builder chain lacks concurrent_updates: {chain}")


def test_concurrent_updates_true():
    node, _ = find_builder_chain()
    assert node is not None
    cur = node.func.value
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        if cur.func.attr == "concurrent_updates":
            assert len(cur.args) == 1, "expected one arg"
            val = cur.args[0]
            assert isinstance(val, ast.Constant) and val.value is True, (
                f"concurrent_updates should be True, got {ast.dump(val)}")
            return
        cur = cur.func.value
    raise AssertionError("concurrent_updates call not found in chain")


def test_no_conversation_handler():
    # PTB warns against concurrent_updates with ConversationHandler
    names = {n.name for n in ast.walk(TREE)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert "ConversationHandler" not in BOT_SRC, \
        "ConversationHandler present — concurrency unsafe"


def test_no_global_locks():
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "Lock", "global Lock() found"


def test_no_time_sleep_in_async_handlers():
    """time.sleep in async code would freeze ALL users even with concurrency."""
    class V(ast.NodeVisitor):
        def __init__(self):
            self.in_async = 0
            self.bad = []

        def visit_AsyncFunctionDef(self, node):
            self.in_async += 1
            self.generic_visit(node)
            self.in_async -= 1

        def visit_Call(self, node):
            f = node.func
            if (self.in_async and isinstance(f, ast.Attribute)
                    and f.attr == "sleep"
                    and isinstance(f.value, ast.Name) and f.value.id == "time"):
                self.bad.append(f"line {node.lineno}")
            self.generic_visit(node)

    v = V()
    v.visit(TREE)
    assert not v.bad, f"time.sleep inside async defs: {v.bad}"


def test_shared_dicts_uid_keyed():
    # _active_downloads / _STREAM_SOURCES / _WATCH_SOURCES / _stream_pending
    # must be keyed per-user so concurrent users never collide
    assert re.search(r"_active_downloads\[[^\]]*token", BOT_SRC), \
        "_active_downloads not token-keyed"
    assert "(uid," in BOT_SRC, "provider registries not uid-keyed"


def test_tmpdir_per_invocation():
    assert "TemporaryDirectory()" in BOT_SRC, "no per-invocation tmpdir"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests)-failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
