#!/usr/bin/env python3
"""
catalog.py | The Santa Skill for Codex, many-products mode. Give it a product list, get a Christmas store.

  discover <SOURCE> [--out DIR] [--max N] [--max-images N]   find the ASINs, write catalog.json
  prepare  <DIR>                     fetch every listing, find shared images, write the plans (needs internet)
  next     <DIR> [--max-images N]    the ONE next task for Codex, product by product, or DONE
  status   <DIR>                     one line per product
  index    <DIR>                     rebuild index.html + report.md from what is on disk

The loop Codex runs: discover, prepare, then `next` and the TASK it prints, again and again until DONE.
Every step is saved on disk, so a run stopped by the plan's usage limit, a closed laptop or a new
Codex session continues with the same `next` command.

SOURCE is one of (the first is the one to ask for):
  product links or ASINs       pasted as they come: any Amazon marketplace, tracking junk and all,
                               app share links (amzn.to, a.co), commas or new lines between them
  a .txt of them               the same, one file
  a Seller Central report      .csv / .tsv / .txt with an asin1 or ASIN column (Active Listings Report),
                               --host www.amazon.co.uk for another marketplace
  the seller's product list    https://www.amazon.com/s?me=SELLERID. Works until Amazon bot-checks it
                               (measured: after four fetches in ten minutes)
  a saved copy of that page    the .html from the browser when curl is bot-checked
  Brand Store and /shop/ pages are refused: JavaScript, and often only part of the catalog.

Caps and stops, by design:
  --max         products taken from a discovery (default 50). Everything found is still listed.
  --max-images  a ceiling on images made with image_gen for this catalog (default: none; the plan's own
                usage limit is the ceiling). Products past it are held and named; raise it and run `next`.
  3 blocks      three Amazon captchas in a row pause `prepare`. The same command an hour later retries.
  duplicates    an image already fetched for another ASIN (bundles, variations) is generated ONCE and
                copied into every folder that uses it.
  one attempt   per page, per image, per generation. One regeneration per failing image, then it is reported.

Layout of the run folder:
  catalog.json                 the state
  index.html                   the whole store, before/after. report.md beside it.
  <ASIN>/originals/ christmas/ plan.json contact-sheet.html report.md usage.jsonl
"""
import argparse
import csv
import html
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import santa  # noqa: E402
from santa import log  # noqa: E402

SELF = Path(__file__).resolve()
DEFAULT_MAX = 50


# ---------------------------------------------------------------- state
def read_catalog(run):
    p = run / "catalog.json"
    if not p.exists():
        sys.exit(f"catalog.json missing in {run}. Run `discover` first.")
    return json.loads(p.read_text())


def write_catalog(run, cat):
    cat["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (run / "catalog.json").write_text(json.dumps(cat, indent=2, ensure_ascii=False))


def product_dir(run, asin):
    return run / asin


# ---------------------------------------------------------------- discover
def parse_storefront(page):
    """ASINs in gallery order from a search-results page, organic results only."""
    asins = []
    for m in re.finditer(r'<div[^>]+data-component-type="s-search-result"[^>]*data-asin="([A-Z0-9]{10})"', page):
        a = m.group(1)
        if a not in asins:
            asins.append(a)
    if not asins:  # attribute order differs on some layouts
        for m in re.finditer(r'data-asin="([A-Z0-9]{10})"[^>]*data-component-type="s-search-result"', page):
            a = m.group(1)
            if a not in asins:
                asins.append(a)
    titles = {}
    for a in asins:
        m = re.search(r'data-asin="%s".*?<h2[^>]*>.*?<span[^>]*>(.*?)</span>' % a, page, re.S)
        if m:
            titles[a] = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    m = re.search(r"([\d,]+)\s+results", page)
    total = int(m.group(1).replace(",", "")) if m else None
    has_next = bool(re.search(r's-pagination-next(?![^>]*s-pagination-disabled)', page))
    return asins, titles, total, has_next


def discover_storefront(url, cap):
    """Walk the storefront pages. One attempt per page, polite pause, stop on a block."""
    m = re.search(r"[?&]me=([A-Z0-9]+)", url)
    seller = m.group(1) if m else None
    base = url.split("#")[0]
    base = re.sub(r"[?&]page=\d+", "", base)
    found, titles, total = [], {}, None
    page_no = 1
    blocked_note = ""
    while True:
        page_url = base + ("&" if "?" in base else "?") + f"page={page_no}"
        log(f"Storefront page {page_no}: {page_url}")
        pg = santa.curl(page_url)
        if santa.offline():
            santa.say_offline()
            sys.exit(3)
        if santa.blocked(pg):
            blocked_note = f"Amazon blocked page {page_no}. Products found so far are kept."
            log("  " + blocked_note)
            break
        asins, t, tot, has_next = parse_storefront(pg)
        if tot and total is None:
            total = tot
        new = [a for a in asins if a not in found]
        found += new
        titles.update(t)
        log(f"  {len(asins)} on the page, {len(new)} new, {len(found)} so far" + (f" of {total}" if total else ""))
        if not new or not has_next or len(found) >= cap and cap > 0:
            break
        if page_no >= 40:  # 40 pages is 1,900+ products; nobody dresses that for Christmas in one go
            break
        page_no += 1
        time.sleep(random.uniform(2.0, 4.0))
    return seller, found, titles, total, blocked_note


def read_asin_file(path, host="www.amazon.com"):
    """A .txt of ASINs/links, or a Seller Central report (tab or comma separated, asin1 / ASIN column).
    Returns ((refs, bad), titles) where refs is [(asin, host)]."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    first = text.splitlines()[0] if text.strip() else ""
    asins, titles = [], {}
    if "\t" in first or ("," in first and "asin" in first.lower()):
        dialect = "excel-tab" if "\t" in first else "excel"
        rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
        cols = {c.lower().strip(): c for c in (rows[0].keys() if rows else [])}
        col = cols.get("asin1") or cols.get("asin") or cols.get("(child) asin") or cols.get("child asin")
        name = cols.get("item-name") or cols.get("item name") or cols.get("title") or cols.get("product name")
        if not col:
            sys.exit(f"No asin1 / ASIN column in {path}. Columns: {list(cols.values())[:12]}")
        for r in rows:
            a = santa.asin_from((r.get(col) or "").strip())
            if a and a not in asins:
                asins.append(a)
                if name and r.get(name):
                    titles[a] = r[name].strip()
        return ([(a, host) for a in asins], []), titles
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    return santa.refs_from_text(text), titles


STORE_HELP = (
    "Give me the products instead: paste the product links or ASINs (your gift-worthy best sellers;\n"
    "20 is a good start), or put them one per line in a .txt. For the whole catalog, Seller Central >\n"
    "Reports > Inventory Reports > Active Listings Report, and give me that file.")


def cmd_discover(args):
    cap = args.max if args.max is not None else DEFAULT_MAX
    src = args.source
    seller, titles, total, note = None, {}, None, ""
    bad = []
    one = src[0] if len(src) == 1 else ""
    f = Path(os.path.expanduser(one)) if one else None
    if f is not None and not f.exists() and one.lower().endswith((".txt", ".csv", ".tsv", ".html", ".htm")):
        sys.exit(f"File not found: {one}")
    if f is not None and f.is_file() and one.lower().endswith((".html", ".htm")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        asins, titles, total, _ = parse_storefront(text)
        refs = [(a, "www.amazon.com") for a in asins]
        m = re.search(r"[?&]me=([A-Z0-9]+)", text)
        seller = m.group(1) if m else None
        source, name = str(f), seller or re.sub(r"[^A-Za-z0-9_-]+", "-", f.stem)[:40]
    elif f is not None and f.is_file():
        (refs, bad), titles = read_asin_file(f, host=args.host)
        source, name = str(f), re.sub(r"[^A-Za-z0-9_-]+", "-", f.stem)[:40] or "list"
    elif one and "amazon." in one and re.search(r"/stores/|/shop/", one):
        log("That is a Brand Store or an influencer page. It is built by JavaScript, it often shows only")
        log("some of the products, and it cannot be read here.")
        log(STORE_HELP)
        sys.exit(2)
    elif one and "amazon." in one and re.search(r"[?&]me=|/s\?", one):
        # the seller's product list (s?me=SELLERID). Works when Amazon does not bot-check it.
        seller, asins, titles, total, note = discover_storefront(one, cap)
        refs = [(a, "www." + (re.search(r"(amazon\.[a-z.]+)", one).group(1))) for a in asins]
        source, name = one, seller or "storefront"
    else:
        refs, bad = santa.refs_from_text(" ".join(src))
        source = "pasted list"
        name = refs[0][0] if len(refs) == 1 else "my-store"
    for item, why in bad:
        log(f"  skipped {item[:80]}: {why}")
    asins = [a for a, _ in refs]
    hosts = dict(refs)
    if not asins:
        log("No products found. " + note)
        if note or source.startswith("http"):
            log("Amazon serves its search pages behind a bot check that plain curl does not always pass.")
        log(STORE_HELP)
        sys.exit(2)
    total_found = total or len(asins)
    taken = asins[:cap] if cap > 0 else asins
    run = Path(args.out) if args.out else Path.cwd() / "santa-catalog" / name
    run.mkdir(parents=True, exist_ok=True)
    cat = {
        "source": source, "seller_id": seller, "discovered_at": datetime.now().isoformat(timespec="seconds"),
        "total_found": total_found, "all_asins": asins, "cap": cap, "note": note,
        "max_images": args.max_images,
        "products": [{"asin": a, "host": hosts.get(a, "www.amazon.com"), "title": titles.get(a, ""),
                      "status": "pending"} for a in taken],
    }
    write_catalog(run, cat)
    log("")
    log(f"Found {total_found} products" + (f" for seller {seller}" if seller else "") + f"; taking {len(taken)}.")
    if len(asins) > len(taken):
        log(f"Taking the first {cap} in the order given. Christmas images pay back on the gift-worthy sellers,")
        log(f"so put those first. To take all {len(asins)}: rerun discover with --max {len(asins)}.")
    if total and total > len(asins):
        log(f"Amazon reports {total} results; {len(asins)} were read before the cap or a block.")
    log(f"Rough estimate: ~{len(taken) * 4} Christmas images (about four per product). Each one is an image_gen")
    log("call on your ChatGPT plan; capacity depends on your plan and other usage.")
    log("Products are finished one at a time, so whatever the limit allows is complete and uploadable.")
    if args.max_images:
        log(f"Image ceiling for this catalog: {args.max_images}.")
    log(f"Run folder: {run}")
    log(f"Next: python3 {SELF} prepare {run}")


# ---------------------------------------------------------------- prepare: fetch + dedupe + plans
def phase_fetch(run, cat):
    """Sequential, polite. Returns "ok", "paused" (3 blocks in a row) or "offline"."""
    todo = [p for p in cat["products"] if p["status"] == "pending"]
    if not todo:
        return "ok"
    log(f"\n== Fetch: {len(todo)} listings")
    streak = 0
    for i, p in enumerate(todo):
        d = product_dir(run, p["asin"])
        if (d / "listing.json").exists() and len(santa.originals_of(d)) >= 2:
            p["status"] = "fetched"
            write_catalog(run, cat)
            continue
        status, n = santa.fetch_listing(p["asin"], d, keep_page=False, host=p.get("host", "www.amazon.com"))
        if status == "offline":
            write_catalog(run, cat)
            santa.say_offline()
            return "offline"
        if status == "blocked":
            streak += 1
            p["status"] = "blocked"
            log(f"  {p['asin']}: BLOCKED ({streak} in a row)")
            write_catalog(run, cat)
            if streak >= 3:
                log("\nThree blocks in a row. Amazon is rate-limiting this address. Fetching is paused.")
                log("Wait an hour and run prepare again; blocked listings are retried once, everything")
                log("already fetched is kept. Or save each blocked listing's images to <ASIN>/originals/ by hand.")
                return "paused"
        else:
            streak = 0
            lst = json.loads((d / "listing.json").read_text())
            p["title"] = p.get("title") or lst.get("title", "")
            p["status"] = "fetched" if status == "ok" else "few"
            if status == "few":
                log(f"  {p['asin']}: fewer than 2 images, nothing to dress")
            write_catalog(run, cat)
        if i < len(todo) - 1:
            time.sleep(random.uniform(1.5, 3.5))
    return "ok"


def phase_dedupe(run, cat):
    """The same image in two listings (bundles, variations) is looked at and generated once."""
    seen = {}
    for p in cat["products"]:
        if p["status"] not in ("fetched", "done", "held"):
            continue
        d = product_dir(run, p["asin"])
        lst = json.loads((d / "listing.json").read_text())
        dups = {}
        for i, u in enumerate(lst.get("image_urls", []), 1):
            fname = f"{i:02d}" + (".png" if u.lower().endswith(".png") else ".jpg")
            key = u.rsplit("/", 1)[-1].split(".")[0]  # the image id, independent of size suffix
            if key in seen and seen[key][0] != p["asin"]:
                dups[fname] = seen[key]
            else:
                seen.setdefault(key, (p["asin"], fname))
        if dups:
            p["duplicates"] = {k: f"{v[0]}/{v[1]}" for k, v in dups.items()}
        plan = santa.ensure_plan(d)
        for img in plan["images"]:
            src = (p.get("duplicates") or {}).get(img["file"])
            if src and img.get("action") in ("TODO", "", None):
                img.update({"role": "duplicate", "action": "skip", "reason": f"same image as {src}",
                            "duplicate_of": src})
        santa.write_plan(d, plan)
    write_catalog(run, cat)


def cmd_prepare(args):
    run = Path(args.run)
    cat = read_catalog(run)
    for p in cat["products"]:   # a paused run retries its blocked listings once
        if p["status"] == "blocked" and not p.get("retried"):
            p["status"], p["retried"] = "pending", True
    outcome = phase_fetch(run, cat)
    if outcome == "offline":
        sys.exit(3)
    phase_dedupe(run, cat)
    ok = [p for p in cat["products"] if p["status"] == "fetched"]
    bad = [p for p in cat["products"] if p["status"] in ("blocked", "few")]
    log(f"\n{len(ok)} listings ready, {len(bad)} not fetched ({', '.join(p['asin'] + ' ' + p['status'] for p in bad) or 'none'}).")
    write_index(run, cat)
    log(f"Next: python3 {SELF} next {run.resolve()}")


# ---------------------------------------------------------------- next: one task at a time
def made_count(run, cat):
    return sum(len(santa.images_made(product_dir(run, p["asin"]))) for p in cat["products"])


def finish_product(run, p):
    """Copy duplicate outputs into this folder, write its sheet, mark it done."""
    d = product_dir(run, p["asin"])
    plan = santa.read_plan(d)
    for img in plan["images"]:
        src = img.get("duplicate_of")
        if not src:
            continue
        s_asin, s_file = src.split("/")
        sd = product_dir(run, s_asin)
        if not (sd / "plan.json").exists():
            continue
        s_img = next((i for i in santa.read_plan(sd)["images"] if i["file"] == s_file), None)
        s_out = santa.out_path(sd, s_file)
        if s_img and s_img.get("action") == "transform" and s_out.exists():
            (d / "christmas").mkdir(exist_ok=True)
            shutil.copy(s_out, santa.out_path(d, img["file"]))
            img.update({"action": "transform", "scene": s_img.get("scene", ""), "stage_ready": s_img.get("stage_ready"),
                        "verdict": f"copied from {src}. " + s_img.get("verdict", ""), "reason": ""})
        elif s_img:
            img["reason"] = f"same image as {src}: " + (s_img.get("reason") or "not generated")
    santa.write_plan(d, plan)
    santa.write_sheet(d)
    p["images"] = len(plan["images"])
    p["made"] = len(santa.images_made(d))
    p["ready"] = sum(1 for i in plan["images"] if i.get("stage_ready") is True)
    p["transforms"] = sum(1 for i in plan["images"] if i.get("action") == "transform")
    p["generated"] = sum(1 for i in plan["images"] if i.get("action") == "transform"
                         and santa.out_path(d, i["file"]).exists())
    p["status"] = "done"


def cmd_next(args):
    run = Path(args.run)
    cat = read_catalog(run)
    if args.max_images is not None:
        cat["max_images"] = args.max_images or None
        write_catalog(run, cat)
    if any(p["status"] == "pending" for p in cat["products"]):
        log(f"TASK prepare: python3 {SELF} prepare {run.resolve()}   (it needs internet to read Amazon)")
        return
    cap = cat.get("max_images")
    for p in cat["products"]:
        if p["status"] not in ("fetched", "held"):
            continue
        d = product_dir(run, p["asin"])
        made = made_count(run, cat)
        room = None if not cap else max(0, cap - made)
        can_make = room is None or room > 0
        step, _ = santa.next_step(d, allow_generate=can_make)
        if step == "finished":
            if santa.pending_jobs(d):     # images still waiting and the ceiling is reached
                p["status"] = "held"
                write_catalog(run, cat)
                continue
            finish_product(run, p)
            write_catalog(run, cat)
            continue
        if p["status"] == "held":
            p["status"] = "fetched"
            write_catalog(run, cat)
        done = sum(1 for q in cat["products"] if q["status"] == "done")
        log(f"Product {done + 1} of {len(cat['products'])}: {p['asin']}  {p.get('title', '')[:70]}")
        log(f"Images made so far in this catalog: {made}" + (f" of a ceiling of {cap}" if cap else ""))
        log("")
        santa.do_next(d, allow_generate=can_make, limit=room)
        log("")
        log(f"When this task is done, run: python3 {SELF} next {run.resolve()}")
        return
    write_index(run, cat)
    done = [p for p in cat["products"] if p["status"] == "done"]
    held = [p for p in cat["products"] if p["status"] == "held"]
    log("DONE")
    log(f"{len(done)}/{len(cat['products'])} products dressed, {sum(p.get('generated', 0) for p in done)} Christmas "
        f"images ({sum(p.get('ready', 0) for p in done)} passed every check), {made_count(run, cat)} made with image_gen.")
    if held:
        log(f"Held back by the image ceiling ({cap}): {', '.join(p['asin'] for p in held)}. "
            f"Raise it with: python3 {SELF} next {run.resolve()} --max-images N")
    other = [p for p in cat["products"] if p["status"] in ("blocked", "few")]
    if other:
        log("Not dressed: " + ", ".join(f"{p['asin']} ({p['status']})" for p in other))
    log(f"Open: {(run / 'index.html').resolve()}")


# ---------------------------------------------------------------- status / index
def cmd_status(args):
    run = Path(args.run)
    cat = read_catalog(run)
    log(f"{cat.get('source')}  ({len(cat['products'])} of {cat.get('total_found')} products)")
    for p in cat["products"]:
        log(f"  {p['asin']}  {p['status']:<8} {p.get('transforms', '-')!s:>3} planned "
            f"{p.get('generated', '-')!s:>3} made {p.get('ready', '-')!s:>3} ready  {p.get('title', '')[:60]}")
    cap = cat.get("max_images")
    log(f"images made with image_gen: {made_count(run, cat)}" + (f" (ceiling {cap})" if cap else ""))


# Open the page with #tour at the end of the address and it walks itself: product by product,
# flipping a few images to the original and back. For screen recordings and demos; a normal
# visit is untouched.
TOUR_JS = """<script>
(function () {
  if (location.hash !== '#tour') return;
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card:not(.pending)'));
  (async function () {
    await wait(2500);
    for (var i = 0; i < cards.length; i++) {
      cards[i].scrollIntoView({behavior: 'smooth', block: 'center'});
      await wait(1900);
      var tiles = cards[i].querySelectorAll('.tile');
      if (tiles.length && i % 2 === 0) {
        var t = tiles[Math.min(1, tiles.length - 1)];
        t.classList.add('peek'); await wait(1500); t.classList.remove('peek'); await wait(700);
      } else { await wait(700); }
    }
    window.scrollTo({top: 0, behavior: 'smooth'});
  })();
})();
</script>"""


def write_index(run, cat):
    cards = []
    n_products = n_images = n_ready = 0
    made = made_count(run, cat)
    for p in cat["products"]:
        d = product_dir(run, p["asin"])
        title = html.escape(p.get("title") or p["asin"])
        if p["status"] != "done":
            cards.append(f'<section class="card pending"><h2>{title}</h2><p class="meta">{p["asin"]} &middot; {p["status"]}</p></section>')
            continue
        plan = santa.read_plan(d)
        n_products += 1
        tiles = []
        for img in plan["images"]:
            out = santa.out_path(d, img["file"])
            if img.get("action") != "transform" or not out.exists():
                continue
            n_images += 1
            if img.get("stage_ready"):
                n_ready += 1
            a = santa.thumb_b64(out, 520)
            b = santa.thumb_b64(d / "originals" / img["file"], 520)
            flag = "" if img.get("stage_ready") is not False else '<span class="flag">check</span>'
            tiles.append(f'<figure class="tile"><img class="after" src="{a}" alt=""><img class="before" src="{b}" alt="">{flag}</figure>')
        if not tiles:
            why = "; ".join(f"{i['file']}: {i.get('reason') or i.get('role')}" for i in plan["images"][1:]) or "no secondary images"
            tiles.append(f'<p class="meta">Nothing to dress here. {html.escape(why)}</p>')
        main = d / "originals" / plan["images"][0]["file"] if plan["images"] else None
        main_b64 = santa.thumb_b64(main, 400) if main and main.exists() else ""
        cards.append(f"""<section class="card">
  <div class="head"><img class="main" src="{main_b64}" alt=""><div><h2>{title}</h2>
  <p class="meta">{p['asin']} &middot; {len(tiles)} Christmas images &middot; <a href="{p['asin']}/contact-sheet.html">before / after sheet</a></p></div></div>
  <div class="tiles">{''.join(tiles)}</div>
</section>""")
    src = html.escape(str(cat.get("source", "")))
    who = cat.get("seller_id") or run.name
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Christmas store: {html.escape(who)}</title>
<style>
:root{{--bg:#0f1a14;--card:#16241c;--ink:#f4efe6;--mute:#a7b3aa;--gold:#e0b45a;--red:#c8433a}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1400px;margin:0 auto;padding:32px 20px 80px}}
h1{{font-size:30px;margin:0 0 6px;letter-spacing:.2px}} h1 span{{color:var(--gold)}}
.sub{{color:var(--mute);margin:0 0 30px}} .sub b{{color:var(--ink)}}
.card{{background:var(--card);border:1px solid #24352b;border-radius:16px;padding:18px;margin-bottom:22px}}
.card.pending{{opacity:.55}}
.head{{display:flex;gap:16px;align-items:center;margin-bottom:14px}}
.head .main{{width:84px;height:84px;object-fit:contain;background:#fff;border-radius:10px;flex:none}}
h2{{font-size:17px;margin:0 0 2px;font-weight:600}} .meta{{color:var(--mute);margin:0;font-size:13px}} .meta a{{color:var(--gold)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}}
.tile{{margin:0;position:relative;aspect-ratio:1;border-radius:12px;overflow:hidden;background:#fff}}
.tile img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:opacity .25s}}
.tile .before{{opacity:0}} .tile:hover .before{{opacity:1}}
.tile::after{{content:"hover: before";position:absolute;left:8px;bottom:8px;font-size:11px;color:#fff;background:rgba(0,0,0,.45);padding:2px 7px;border-radius:999px;opacity:0;transition:opacity .2s}}
.tile:hover::after{{opacity:1;content:"before"}}
.flag{{position:absolute;top:8px;right:8px;font-size:11px;background:var(--red);color:#fff;padding:2px 8px;border-radius:999px}}
.tile.peek .before{{opacity:1}} .tile.peek::after{{opacity:1;content:"before"}}
.foot{{color:var(--mute);font-size:13px;margin-top:30px}}
</style></head><body><div class="wrap">
<h1>Your store, <span>dressed for Christmas</span></h1>
<p class="sub"><b>{n_products}</b> products &middot; <b>{n_images}</b> Christmas images &middot; {n_ready} passed every check &middot; {made} made with image_gen on your ChatGPT plan &middot; main images untouched &middot; source: {src}</p>
{''.join(cards)}
<p class="foot">Generated with The Santa Skill for Codex, catalog mode. Hover any image for the original. Each product folder holds originals/, christmas/ and its own before/after sheet.</p>
</div>{TOUR_JS}</body></html>"""
    (run / "index.html").write_text(page)

    lines = [f"# Santa catalog report: {who}", "", f"Source: {cat.get('source')}",
             f"Products found: {cat.get('total_found')}. Taken: {len(cat['products'])}. Done: {n_products}.",
             f"Christmas images: {n_images}. Stage ready: {n_ready}. Made with Codex image_gen: {made} "
             f"(ChatGPT plan usage, no API bill).", "",
             "| ASIN | status | images | transformed | ready | made | title |", "|---|---|---|---|---|---|---|"]
    for p in cat["products"]:
        lines.append(f"| {p['asin']} | {p['status']} | {p.get('images', '')} | {p.get('transforms', '')} | "
                     f"{p.get('ready', '')} | {p.get('made', '')} | {(p.get('title') or '')[:70]} |")
    if cat.get("note"):
        lines += ["", cat["note"]]
    lines += ["", "Each <ASIN>/ folder: originals/ (the way back in January), christmas/ (the uploads), "
              "contact-sheet.html (before/after with verdicts), report.md."]
    (run / "report.md").write_text("\n".join(lines) + "\n")
    log(f"Wrote {run / 'index.html'} and {run / 'report.md'}")


def cmd_index(args):
    run = Path(args.run)
    write_index(run, read_catalog(run))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("source", nargs="+"); d.add_argument("--out")
    d.add_argument("--max", type=int, help=f"products to take (default {DEFAULT_MAX}, 0 = all)")
    d.add_argument("--max-images", type=int, help="ceiling on image_gen calls for this catalog (default none)")
    d.add_argument("--host", default="www.amazon.com", help="marketplace for a Seller Central report (www.amazon.co.uk, ...)")
    d.set_defaults(fn=cmd_discover)
    p = sub.add_parser("prepare"); p.add_argument("run"); p.set_defaults(fn=cmd_prepare)
    n = sub.add_parser("next"); n.add_argument("run")
    n.add_argument("--max-images", type=int, help="set or change the image ceiling (0 = none)")
    n.set_defaults(fn=cmd_next)
    s = sub.add_parser("status"); s.add_argument("run"); s.set_defaults(fn=cmd_status)
    i = sub.add_parser("index"); i.add_argument("run"); i.set_defaults(fn=cmd_index)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
