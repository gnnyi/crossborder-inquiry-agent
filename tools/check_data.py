"""Referential-integrity checks for the eval world.

    python tools/check_data.py            # env + KB (+ messages if present)
    python tools/check_data.py --stats    # also print message language/structure distribution

Prints only aggregate counts for messages, never per-message sealed annotations, so it is safe to run
before blind labelling.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sandbox import kb as kbmod  # noqa: E402

EVAL = REPO_ROOT / "eval"
ENV = EVAL / "env"
RESERVED_EMAIL = re.compile(r"^[a-z0-9._%+'-]+@(?:[a-z0-9-]+\.)*(example\.(com|net|org)|[a-z0-9-]+\.example)$", re.I)
ORDER_STATUSES = {"unfulfilled", "processing", "shipped", "delivered", "returned", "refund_pending", "exception", "cancelled"}


def money(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load_env():
    meta = json.loads((ENV / "meta.json").read_text(encoding="utf-8"))
    products = json.loads((ENV / "products.json").read_text(encoding="utf-8"))
    orders = json.loads((ENV / "orders.json").read_text(encoding="utf-8"))
    tracking = json.loads((ENV / "tracking.json").read_text(encoding="utf-8"))
    return meta, products, orders, tracking


def price_at(product: dict, date: str) -> float | None:
    price = None
    for h in sorted(product["price_history"], key=lambda h: h["effective_from"]):
        if h["effective_from"] <= date:
            price = h["price_usd"]
    return price


def check_products(c: Checker, products: list[dict], as_of: str) -> dict[str, dict]:
    by_sku: dict[str, dict] = {}
    required = ["sku", "name", "category", "price_usd", "compare_at_usd", "material", "colors", "sizes", "stock", "final_sale", "fit_note", "price_history"]
    for p in products:
        for f in required:
            if f not in p:
                c.err(f"product {p.get('sku')}: missing field {f}")
        sku = p.get("sku")
        if sku in by_sku:
            c.err(f"duplicate sku {sku}")
        by_sku[sku] = p
        if not re.fullmatch(r"[A-Z]{3}-\d{3}", sku or ""):
            c.err(f"product {sku}: sku format")
        hist = sorted(p.get("price_history", []), key=lambda h: h["effective_from"])
        if not hist or hist[-1]["price_usd"] != p["price_usd"]:
            c.err(f"product {sku}: price_usd != latest price_history")
        expected_compare = hist[-2]["price_usd"] if len(hist) > 1 else None
        if p.get("compare_at_usd") != expected_compare:
            c.err(f"product {sku}: compare_at_usd {p.get('compare_at_usd')} != previous price {expected_compare}")
        if hist and hist[-1]["effective_from"] > as_of:
            c.err(f"product {sku}: price change after as_of")
        if not 40 <= p["price_usd"] <= 200:
            c.err(f"product {sku}: price {p['price_usd']} outside $40-$200")
        if set(p["stock"]) != set(p["sizes"]):
            c.err(f"product {sku}: stock keys != sizes")
        fs_since = p.get("final_sale_since")
        if p["final_sale"] != (fs_since is not None and fs_since <= as_of):
            c.err(f"product {sku}: final_sale inconsistent with final_sale_since")
    if len(products) != 30:
        c.err(f"expected 30 products, found {len(products)}")
    return by_sku


def check_catalog_index(c: Checker, by_sku: dict[str, dict], kb: dict[str, list[str]]) -> None:
    lines = kb.get("catalog_index", [])
    seen = set()
    for n, line in enumerate(lines, 1):
        m = re.match(r"- ([A-Z]{3}-\d{3}) \| (.+?) \| (.+?) \| \$([\d.]+)(?: \| (.*))?$", line)
        if not m:
            continue
        sku, name, _cat, price, notes = m.groups()
        seen.add(sku)
        p = by_sku.get(sku)
        if not p:
            c.err(f"catalog_index:{n}: unknown sku {sku}")
            continue
        if p["name"] != name:
            c.err(f"catalog_index:{n}: name '{name}' != products.json '{p['name']}'")
        if float(price) != p["price_usd"]:
            c.err(f"catalog_index:{n}: price {price} != {p['price_usd']}")
        if ("Final Sale" in (notes or "")) != p["final_sale"]:
            c.err(f"catalog_index:{n}: final sale marker mismatch for {sku}")
        if p["compare_at_usd"] and f"was ${p['compare_at_usd']:.2f}" not in (notes or ""):
            c.err(f"catalog_index:{n}: missing 'was' price for {sku}")
    missing = set(by_sku) - seen
    if missing:
        c.err(f"catalog_index: missing skus {sorted(missing)}")


def shipping_fee(meta: dict, created: str, country: str, method: str, merch: float) -> float:
    mirrors = meta["machine_readable_mirrors"]
    schedules = sorted(mirrors["shipping_fee_schedules"], key=lambda s: s["effective_from"])
    sched = None
    for s in schedules:
        if s["effective_from"] <= created:
            sched = s
    rule = sched[method]["US" if country == "US" else "INTL"]
    if rule["free_from"] is not None and merch >= rule["free_from"]:
        return 0.0
    return rule["fee"]


def check_orders(c: Checker, meta: dict, orders: list[dict], by_sku: dict[str, dict], tracking: dict[str, dict]) -> None:
    as_of = meta["as_of"]
    codes = meta["machine_readable_mirrors"]["discount_codes"]
    ids = [o["order_id"] for o in orders]
    if len(orders) != 60:
        c.err(f"expected 60 orders, found {len(orders)}")
    if len(set(ids)) != len(ids):
        c.err("duplicate order ids")
    if sorted(ids) != [str(i) for i in range(10201, 10201 + len(orders))]:
        c.err("order ids must be consecutive 5-digit numbers starting at 10201")
    required = ["order_id", "email", "customer_name", "created_at", "status", "items", "discount_code", "subtotal",
                "shipping_fee", "total", "currency", "ship_to", "shipping_method", "shipped_at", "delivered_at",
                "carrier", "tracking_number"]
    statuses = Counter()
    first_order: dict[str, str] = {}
    for o in sorted(orders, key=lambda o: o["created_at"]):
        first_order.setdefault(o["email"].lower(), o["order_id"])
    referenced_tn = set()
    prev_created = ""
    for o in sorted(orders, key=lambda o: o["order_id"]):
        oid = o["order_id"]
        for f in required:
            if f not in o:
                c.err(f"order {oid}: missing {f}")
        statuses[o["status"]] += 1
        if o["status"] not in ORDER_STATUSES:
            c.err(f"order {oid}: unknown status {o['status']}")
        if not RESERVED_EMAIL.match(o["email"]):
            c.err(f"order {oid}: email {o['email']} is not on a reserved example domain")
        for f in ["name", "country", "city", "postal", "line1"]:
            if not o["ship_to"].get(f):
                c.err(f"order {oid}: ship_to.{f} empty")
        created = day(o["created_at"])
        if o["created_at"] < prev_created:
            c.warn(f"order {oid}: created_at earlier than previous order id")
        prev_created = o["created_at"]
        if created > as_of:
            c.err(f"order {oid}: created after as_of")
        # items and money
        subtotal = 0.0
        eligible = 0.0
        for i, it in enumerate(o["items"]):
            p = by_sku.get(it["sku"])
            if not p:
                c.err(f"order {oid}: unknown sku {it['sku']}")
                continue
            if it["size"] not in p["sizes"]:
                c.err(f"order {oid} line {i}: size {it['size']} not offered for {it['sku']}")
            if it["color"] not in p["colors"]:
                c.err(f"order {oid} line {i}: color {it['color']} not offered for {it['sku']}")
            if it.get("name") and it["name"] != p["name"]:
                c.err(f"order {oid} line {i}: name mismatch")
            if it["unit_price"] != price_at(p, created):
                c.err(f"order {oid} line {i}: unit_price {it['unit_price']} != catalogue price {price_at(p, created)} on {created}")
            fs = p.get("final_sale_since")
            if it.get("final_sale") != (fs is not None and fs <= created):
                c.err(f"order {oid} line {i}: final_sale snapshot wrong")
            subtotal += it["unit_price"] * it["qty"]
            if not it.get("final_sale"):
                eligible += it["unit_price"] * it["qty"]
        if money(subtotal) != o["subtotal"]:
            c.err(f"order {oid}: subtotal {o['subtotal']} != sum of lines {money(subtotal)}")
        disc = 0.0
        code = o.get("discount_code")
        if code:
            rule = codes.get(code)
            if not rule:
                c.err(f"order {oid}: unknown discount code {code}")
            else:
                if created < rule["valid_from"] or (rule["valid_to"] and created > rule["valid_to"]):
                    c.err(f"order {oid}: code {code} used outside validity")
                if eligible < rule["min_eligible_subtotal"]:
                    c.err(f"order {oid}: code {code} below minimum")
                if rule["first_order_only"] and first_order[o["email"].lower()] != oid:
                    c.err(f"order {oid}: {code} is first-order only")
                disc = money(eligible * rule["percent"] / 100)
        if o.get("discount_amount", 0.0) != disc:
            c.err(f"order {oid}: discount_amount {o.get('discount_amount')} != expected {disc}")
        merch = money(o["subtotal"] - disc)
        fee = shipping_fee(meta, created, o["ship_to"]["country"], o["shipping_method"], merch)
        if o["shipping_fee"] != fee:
            c.err(f"order {oid}: shipping_fee {o['shipping_fee']} != policy fee {fee}")
        if money(merch + o["shipping_fee"]) != o["total"]:
            c.err(f"order {oid}: total {o['total']} != subtotal - discount + shipping")
        # fulfilment consistency
        ships = o.get("shipments", [])
        st = o["status"]
        if st in {"unfulfilled", "processing", "cancelled"}:
            if ships or o["tracking_number"] or o["shipped_at"]:
                c.err(f"order {oid}: status {st} but has shipment data")
        else:
            if not ships:
                c.err(f"order {oid}: status {st} but no shipments")
        if st == "cancelled" and not o.get("cancellation"):
            c.err(f"order {oid}: cancelled without cancellation info")
        if st == "exception" and not o.get("exception"):
            c.err(f"order {oid}: exception without exception info")
        if st in {"returned", "refund_pending"}:
            r = o.get("return_info")
            if not r:
                c.err(f"order {oid}: {st} without return_info")
            else:
                want = "refunded" if st == "returned" else "pending"
                if r["refund_status"] != want:
                    c.err(f"order {oid}: return_info.refund_status {r['refund_status']} != {want}")
                if not o["delivered_at"] or day(r["requested_at"]) < day(o["delivered_at"]):
                    c.err(f"order {oid}: return requested before delivery")
                elif (dt.date.fromisoformat(day(r["requested_at"])) - dt.date.fromisoformat(day(o["delivered_at"]))).days > 30:
                    c.err(f"order {oid}: return requested outside 30-day window")
                seq = [r["requested_at"], r.get("received_at"), r.get("refunded_at")]
                seq = [s for s in seq if s]
                if seq != sorted(seq) or any(day(s) > as_of for s in seq):
                    c.err(f"order {oid}: return dates out of order or after as_of")
        line_cover = []
        all_delivered = bool(ships)
        for s in ships:
            line_cover.extend(s["line_indexes"])
            tn = s["tracking_number"]
            referenced_tn.add(tn)
            t = tracking.get(tn)
            if not t:
                c.err(f"order {oid}: tracking {tn} missing in tracking.json")
                continue
            if t["order_id"] != oid:
                c.err(f"tracking {tn}: order_id {t['order_id']} != {oid}")
            if t["carrier"] != s["carrier"]:
                c.err(f"tracking {tn}: carrier mismatch")
            if t["status"] != s["status"]:
                c.err(f"order {oid}: shipment status {s['status']} != tracking status {t['status']}")
            ts = [e["ts"] for e in t["events"]]
            if ts != sorted(ts):
                c.err(f"tracking {tn}: events not chronological")
            if any(day(x) > as_of for x in ts):
                c.err(f"tracking {tn}: events after as_of")
            picked = [e["ts"] for e in t["events"] if e["code"] == "PICKED_UP"]
            if not picked or picked[0] != s["shipped_at"]:
                c.err(f"order {oid}: shipped_at != first PICKED_UP scan")
            delivered = [e["ts"] for e in t["events"] if e["code"] == "DELIVERED"]
            if (delivered[0] if delivered else None) != s["delivered_at"]:
                c.err(f"order {oid}: shipment delivered_at != DELIVERED scan")
            if s["delivered_at"] is None:
                all_delivered = False
            if s["shipped_at"] and day(s["shipped_at"]) < created:
                c.err(f"order {oid}: shipped before created")
            if s["delivered_at"] and s["delivered_at"] < s["shipped_at"]:
                c.err(f"order {oid}: delivered before shipped")
        if ships:
            full = sorted(set(line_cover)) == list(range(len(o["items"])))
            first = ships[0]
            if o["tracking_number"] != first["tracking_number"] or o["carrier"] != first["carrier"] or o["shipped_at"] != first["shipped_at"]:
                c.err(f"order {oid}: top-level shipping fields must mirror first shipment")
            order_delivered = all_delivered and full
            if st in {"delivered", "returned", "refund_pending"} and not order_delivered:
                c.err(f"order {oid}: status {st} but not all lines delivered")
            if st in {"shipped", "exception"} and order_delivered:
                c.err(f"order {oid}: status {st} but every line delivered")
            want_delivered_at = max(s["delivered_at"] for s in ships) if order_delivered else None
            if o["delivered_at"] != want_delivered_at:
                c.err(f"order {oid}: order delivered_at {o['delivered_at']} != {want_delivered_at}")
    for tn in tracking:
        if tn not in referenced_tn:
            c.err(f"tracking {tn}: not referenced by any order")
    missing_status = ORDER_STATUSES - set(statuses)
    if missing_status:
        c.err(f"order statuses not covered: {sorted(missing_status)}")


def check_kb(c: Checker) -> dict[str, list[str]]:
    kb = kbmod.load_kb()
    if not 8 <= len(kb) <= 12:
        c.err(f"KB should have 8-12 docs, found {len(kb)}")
    for doc_id, lines in kb.items():
        if len(lines) < 3 or lines[1] != f"- doc_id: {doc_id}":
            c.err(f"kb {doc_id}: line 2 must be '- doc_id: {doc_id}'")
        raw = (kbmod.KB_DIR / f"{doc_id}.md").read_text(encoding="utf-8").split("\n")
        for n in range(1, len(lines) + 1):
            if kbmod.get_lines(doc_id, n, n, kb) != [raw[n - 1].rstrip("\r")]:
                c.err(f"kb {doc_id}: line {n} does not round-trip")
                break
        kbmod.render_with_line_numbers(doc_id, kb)
        for n, line in enumerate(lines, 1):
            if not line.strip():
                c.warn(f"kb {doc_id}:{n}: blank line")
            elif not (line.startswith("#") or line.startswith("- ")):
                c.warn(f"kb {doc_id}:{n}: line is not a heading or a '- ' fact")
    return kb


# ---------------------------------------------------------------- messages
ORDER_TOKEN = re.compile(r"(?<![\w.])(?:#|NW-?|nw-?|No\.?\s?|Nr\.?\s?|n[º°o]\.?\s?)?([0-9Oo]{5,6})(?![\w])")
EMAIL_TOKEN = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKU_TOKEN = re.compile(r"\b([A-Z]{3}-\d{3})\b")
LEAK_KEYS = {"intents", "intent", "priority", "risk_flags", "risk", "route", "gold_route", "difficulty_tags", "tags",
             "scenario", "split", "expected_tool_calls", "must_address", "labels", "notes"}
MSG_KEYS = {"msg_id", "channel", "lang", "from", "subject", "received_at", "thread"}


def order_tokens(text: str) -> list[str]:
    """Order-number-looking tokens with letter O folded to 0 (digits only, 5-6 chars)."""
    out = []
    for m in ORDER_TOKEN.finditer(text):
        raw = m.group(1)
        if not any(ch.isdigit() for ch in raw) or sum(ch.isdigit() for ch in raw) < 3:
            continue
        out.append(raw.replace("O", "0").replace("o", "0"))
    return out


def check_messages(c: Checker, orders: list[dict], by_sku: dict[str, dict], stats: bool) -> None:
    path = EVAL / "messages.jsonl"
    if not path.exists():
        c.warn("eval/messages.jsonl not present yet")
        return
    msgs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    notes_path = EVAL / "_sealed" / "generation_notes.jsonl"
    notes = {}
    if notes_path.exists():
        for line in notes_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                n = json.loads(line)
                notes[n["msg_id"]] = n
    order_ids = {o["order_id"] for o in orders}
    order_email = {o["order_id"]: o["email"].lower() for o in orders}
    emails = {o["email"].lower() for o in orders}
    ids = [m["msg_id"] for m in msgs]
    if len(set(ids)) != len(ids):
        c.err("messages: duplicate msg_id")
    for m in msgs:
        mid = m.get("msg_id")
        extra = set(m) - MSG_KEYS
        if extra & LEAK_KEYS or extra:
            c.err(f"{mid}: unexpected keys {sorted(extra)} (possible label leak)")
        for k in ["msg_id", "channel", "lang", "from", "received_at", "thread"]:
            if k not in m:
                c.err(f"{mid}: missing {k}")
        if m.get("channel") not in {"email", "site_chat", "instagram_dm"}:
            c.err(f"{mid}: bad channel")
        if (m.get("channel") == "email") != ("subject" in m):
            c.err(f"{mid}: subject must be present for email only")
        if m.get("lang") not in {"en", "de", "es", "fr"}:
            c.err(f"{mid}: bad lang")
        thread = m.get("thread", [])
        if not thread or thread[-1]["role"] != "customer":
            c.err(f"{mid}: thread must end with a customer turn")
        ts = [t["ts"] for t in thread]
        if ts != sorted(ts):
            c.err(f"{mid}: thread timestamps out of order")
        if thread and m["received_at"] != thread[-1]["ts"]:
            c.err(f"{mid}: received_at must equal last turn ts")
        if m.get("received_at", "9")[:10] > "2026-10-20":
            c.err(f"{mid}: received after as_of")
        for t in thread:
            if t["role"] not in {"customer", "agent"}:
                c.err(f"{mid}: bad role {t['role']}")
        if m.get("channel") == "email" and not RESERVED_EMAIL.match(m.get("from", "")):
            c.err(f"{mid}: sender {m.get('from')} not on a reserved domain")
        text = "\n".join(t["text"] for t in thread) + "\n" + m.get("subject", "")
        for e in EMAIL_TOKEN.findall(text):
            if not RESERVED_EMAIL.match(e):
                c.err(f"{mid}: email {e} in text not on a reserved domain")
        note = notes.get(mid)
        if note is None:
            if notes:
                c.err(f"{mid}: no generation note")
            continue
        refs = note.get("refs", {})
        bad = set(refs.get("bad_order_refs", []))
        declared = set(refs.get("orders", []))
        for tok in order_tokens(text):
            if tok in order_ids:
                if tok not in declared:
                    c.err(f"{mid}: mentions order {tok} not declared in sealed refs.orders")
            elif tok not in bad:
                c.err(f"{mid}: order-like token {tok} neither exists nor is declared as intentionally wrong")
        for oid in declared:
            if oid not in order_ids:
                c.err(f"{mid}: sealed refs.orders has unknown order {oid}")
        for b in bad:
            if b in order_ids:
                c.err(f"{mid}: bad_order_ref {b} actually exists")
        for sku in SKU_TOKEN.findall(text):
            if sku not in by_sku:
                c.err(f"{mid}: unknown SKU {sku}")
        sender = m.get("from", "").lower()
        if refs.get("sender_matches_order") is True:
            if not any(order_email[o] == sender for o in declared):
                c.err(f"{mid}: sealed says sender matches an order, but {sender} owns none of {sorted(declared)}")
        if refs.get("sender_matches_order") is False and "@" in sender:
            if any(order_email[o] == sender for o in declared):
                c.err(f"{mid}: sealed says sender mismatch, but sender owns a referenced order")
        if refs.get("sender_has_orders") is True and sender not in emails:
            c.err(f"{mid}: sealed says sender has orders but {sender} has none")
        if refs.get("sender_has_orders") is False and sender in emails:
            c.err(f"{mid}: sealed says sender has no orders but {sender} has some")
        for call in note.get("expected_tool_calls", []):
            a = call["args"]
            if call["name"] in {"get_order", "get_tracking"} and a["order_id"] not in order_ids:
                c.err(f"{mid}: expected call {call} references unknown order")
            if call["name"] == "get_product" and a["sku"] not in by_sku:
                c.err(f"{mid}: expected call {call} references unknown sku")
            if call["name"] == "find_orders_by_email" and a["email"].lower() not in emails:
                c.err(f"{mid}: expected call {call}: email has no orders (should the call still be expected?)")
    if notes and set(notes) != set(ids):
        c.err(f"generation_notes / messages id mismatch: {sorted(set(notes) ^ set(ids))[:10]}")
    if stats:
        print_message_stats(msgs)


def message_structure(m: dict) -> str:
    n = len(m["thread"])
    if n >= 5:
        return "long_thread(>=5 turns)"
    if n >= 2:
        return "multi_turn(2-4 turns)"
    return "single"


def print_message_stats(msgs: list[dict]) -> None:
    total = len(msgs)

    def show(title: str, counter: Counter) -> None:
        print(f"\n{title}")
        for k, v in sorted(counter.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<26} {v:>4}  {v / total:6.1%}")

    print(f"\nmessages: {total}")
    show("lang", Counter(m["lang"] for m in msgs))
    show("channel", Counter(m["channel"] for m in msgs))
    show("structure", Counter(message_structure(m) for m in msgs))
    multi = sum(1 for m in msgs if len(m["thread"]) >= 2)
    print(f"  multi-turn total (>=2 turns): {multi} ({multi / total:.1%})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stats", action="store_true", help="print message distribution")
    args = ap.parse_args(argv)
    c = Checker()
    meta, products, orders, tracking = load_env()
    as_of = meta["as_of"]
    if as_of != "2026-10-20":
        c.err(f"meta.as_of is {as_of}, expected 2026-10-20")
    kb = check_kb(c)
    by_sku = check_products(c, products, as_of)
    check_catalog_index(c, by_sku, kb)
    check_orders(c, meta, orders, by_sku, tracking)
    check_messages(c, orders, by_sku, args.stats)
    for w in c.warnings:
        print(f"WARN  {w}")
    for e in c.errors:
        print(f"ERROR {e}")
    print(f"\ncheck_data: {len(c.errors)} error(s), {len(c.warnings)} warning(s); "
          f"{len(kb)} KB docs, {len(products)} products, {len(orders)} orders, {len(tracking)} tracking records")
    return 1 if c.errors else 0


if __name__ == "__main__":
    sys.exit(main())
