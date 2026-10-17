"""Rules engine: marks up a hospital bill against a (hard-coded) policy."""

NON_PAYABLE = ["glove", "mask", "shoe cover", "admission kit", "registration",
               "admission fee", "attendant", "toiletr", "sanitizer", "cap "]

DEFAULT_POLICY = {"room_cap_per_day": 4000, "room_days": 3}

SAMPLE = [
    {"n": "Room rent, deluxe, 3 days", "b": 21000, "cat": "room"},
    {"n": "Surgeon\u2019s fee", "b": 60000, "cat": "prop"},
    {"n": "Operation theatre", "b": 35000, "cat": "prop"},
    {"n": "Anaesthetist\u2019s fee", "b": 18000, "cat": "prop"},
    {"n": "Nursing, 3 days", "b": 6000, "cat": "prop"},
    {"n": "Pharmacy", "b": 22400, "cat": "other"},
    {"n": "Blood tests", "b": 6200, "cat": "other"},
    {"n": "Ultrasound abdomen", "b": 2400, "cat": "other"},
    {"n": "Ultrasound abdomen", "b": 2400, "cat": "other"},
    {"n": "Gloves, masks, shoe covers", "b": 4800, "cat": "other"},
    {"n": "Admission kit", "b": 1500, "cat": "other"},
]


def analyze(items, policy=None):
    policy = {**DEFAULT_POLICY, **(policy or {})}
    cap, days = policy["room_cap_per_day"], policy["room_days"]

    # room-rent ratio, taken from the room line
    ratio = 1.0
    for it in items:
        if it.get("cat") == "room" and it["b"] / days > cap:
            ratio = cap / (it["b"] / days)

    seen, out = set(), []
    for it in items:
        name, billed = it["n"], it["b"]
        key = name.strip().lower()
        row = {"n": name, "b": billed, "t": "ok", "pay": billed,
               "h": "Nothing to flag",
               "x": "This line looks payable under the policy."}

        if key in seen:
            row.update(t="dup", pay=0, h="Billed twice",
                       x="The same item appears twice. Ask the hospital to remove one before you pay.")
        elif any(w in key for w in NON_PAYABLE):
            row.update(t="np", pay=0, h="Not payable",
                       x="This looks like a standard non-payable item, so the insurer pays nothing. "
                         "Ask if it is already included in the procedure charge.")
        elif it.get("cat") == "room" and ratio < 1:
            row.update(t="cap", pay=cap * days, h="Room rent is above your cap",
                       x=f"Your policy pays up to \u20b9{cap:,} a day. This room costs more, "
                         f"so the line is cut to \u20b9{cap * days:,}.")
        elif it.get("cat") == "prop" and ratio < 1:
            row.update(t="cap", pay=round(billed * ratio), h="Reduced in proportion",
                       x=f"Because the room is above the cap, related charges are scaled by "
                         f"the same ratio (about {ratio:.0%}).")
        seen.add(key)
        out.append(row)

    total = sum(r["b"] for r in out)
    paid = sum(r["pay"] for r in out)
    return {"items": out, "billed": total, "insurer_pays": paid, "you_pay": total - paid}
