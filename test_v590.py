"""v5.9.0 tests: admin panel + user subscription expiry.

Run: python3 test_v590.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store

PASS = []


def check(name, cond):
    PASS.append(name)
    assert cond, f"FAIL: {name}"


# --- UserStore with temp DATA_DIR ---------------------------------------------
tmp = tempfile.mkdtemp()
store.DATA_DIR = tmp
us = store.UserStore()

# legacy migration: {"allowed": [...]} without meta
import json
json.dump({"allowed": [111]}, open(os.path.join(tmp, "users.json"), "w"))
us2 = store.UserStore()
check("legacy ids preserved", us2.allowed_ids() == {111})
m = us2.meta(111)
check("legacy meta synthesized", m.get("expires") is None)
check("legacy not expired", not us2.is_expired(111))

# add with months
now = time.time()
check("add new", us.add(222, months=3, name="tester") is True)
exp = us.expiry(222)
check("expiry ~90d", abs(exp - (now + 90 * 86400)) < 60)
check("not expired", not us.is_expired(222))
dl = us.days_left(222)
check("days_left ~90", dl is not None and 89 < dl <= 90)
check("re-add existing", us.add(222) is False)

# add unlimited
us.add(333)
check("unlimited expiry None", us.expiry(333) is None)
check("unlimited days_left None", us.days_left(333) is None)
check("unlimited not expired", not us.is_expired(333))

# expired user
us.add(444, months=0.000001)  # ~2.6 sec... use direct meta instead
d = us._data()
d["meta"]["444"]["expires"] = int(time.time()) - 10
us._save_data(d)
check("is_expired true", us.is_expired(444) is True)
check("days_left negative", us.days_left(444) < 0)

# extend from expired -> from now
new_exp = us.extend(444, 1)
check("extend returns ts", new_exp is not None)
check("extend from now", abs(new_exp - (time.time() + 30 * 86400)) < 120)
check("extend clears expired", not us.is_expired(444))

# extend active user adds on top
before = us.expiry(222)
new2 = us.extend(222, 1)
check("extend stacks", abs(new2 - (before + 30 * 86400)) < 120)

# extend unknown user
check("extend unknown None", us.extend(999, 1) is None)

# flags
us.set_flag(222, "warned3")
check("flag set", us.meta(222).get("warned3") is True)

# all_users sorted
ids = [u["id"] for u in us.all_users()]
check("all_users sorted", ids == sorted(ids))
check("all_users has name", any(u["name"] == "tester" for u in us.all_users()))

# remove cleans meta
check("remove", us.remove(222) is True)
check("meta cleaned", us.meta(222) == {})
check("remove unknown", us.remove(222) is False)

# --- bot wiring (static) --------------------------------------------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "bot.py"), encoding="utf-8").read()
for needle, name in [
    ('("extend", extend_cmd)', "extend registered"),
    ('("admin", admin_cmd)', "admin registered"),
    ("async def expiry_job", "expiry_job defined"),
    ("run_repeating(expiry_job", "expiry_job scheduled"),
    ('callback_data="menu:admin"', "menu admin button"),
    ('if uid == OWNER_ID:\n        rows.append([InlineKeyboardButton("👑 Admin"',
     "menu admin owner-only"),
    ('elif action == "admin":', "menu admin handled"),
    ('"extend": (', "help topic extend"),
    ('"admin": (', "help topic admin"),
    ("user_store.is_expired(uid)", "allowed() checks expiry"),
    ("_expired_notice", "expiry notice dedup"),
    ("def _admin_text", "admin dashboard builder"),
]:
    check(name, needle in src)

print(f"✅ v5.9.0 admin+expiry: {len(PASS)} tests passed")
