#!/usr/bin/env python3
"""
santa.py | The Santa Skill for Codex. Christmas versions of Amazon secondary images.

This is the Codex build. The Claude Code build calls the OpenAI API with the user's key. Here
nothing in this file talks to OpenAI: Codex itself does the looking (view_image) and the image
editing (its built-in image_gen tool, on the user's ChatGPT plan). This file does everything that
must not depend on a model: fetching, folders, the rules, the prompts, the measured checks, the
sheets. It tells Codex what to look at and what to generate, and it takes the answers back.

The loop Codex runs:  python3 santa.py next <DIR>   ... do the TASK it prints ...   repeat until DONE.

Subcommands (the run folder is ./santa/<ASIN>):

  fetch    <ASIN|URL|folder> [--out DIR]  download listing images (or copy a folder) to originals/
  next     <DIR>                          the ONE next task for Codex, or DONE (writes the sheet at the end)
  list     <DIR>                          print originals with size and aspect, write the plan.json template
  classify <DIR> [--apply [FILE]]         print the looking task / apply Codex's answers from classify.json
  prepare  <DIR> [--only NAME]            write the image_gen inputs and print one job per transform
  place    <DIR> <NAME> [PATH|--latest]   take image_gen's output into christmas/<NAME>.png
           [--failed REASON]              or record that image_gen refused it (no retry)
  regen    <DIR> <NAME> [--note TEXT]     the one allowed retry: note into the scene, first attempt aside
  check    <DIR>                          mean brightness before vs after (flags a drop over 8%)
  verify   <DIR> [--apply [FILE]]         print the judging task (with advisory scene matching) / apply answers
  sheet    <DIR>                          write contact-sheet.html and report.md

Catalog mode (many products) lives in catalog.py beside this file and reuses these functions.

Env:
  CODEX_HOME   where Codex saves image_gen output (default ~/.codex); place --latest looks in
               $CODEX_HOME/generated_images

Rules the prompt is built from: ../references/christmas-prompt-rules.md
No retries against Amazon. One image_gen call per image, one regeneration per failing image.
"""
import argparse
import base64
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES_FILE = HERE.parent / "references" / "christmas-prompt-rules.md"
SELF = HERE / "santa.py"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Jay's STRONG Christmas (decided 5.9.2026 for the Cyprus talk after a 10-image test: the subtle
# version disappeared at Amazon thumbnail size) and his WINTER conversion for summer scenes. These
# are the prompts that made the Cyprus slides; they reached the skill on 26.9.2026 (Jay: "I want it
# to do these things"). KEEP is the part that never moves. Identical to the Claude Code build.
KEEP = (
    "Keep the product exactly as it is: same shape, color, size, position, angle and label, nothing placed on it, "
    "in front of it or touching it. Keep every existing headline, label and graphic element exactly as written, "
    "same position. Keep every person's face, identity, pose, hands and expression exactly the same. Keep the "
    "camera angle, framing and lens. Photorealistic, matching lighting and shadows, no cartoon items, no glitter, "
    "no floating objects, no added text or logos. The result must be as bright as the original or brighter, warm "
    "and festive."
)
STRONG_TEMPLATE = (
    "Create a rich, unmistakable Christmas version of this exact image, one that reads as Christmas even as a "
    "small thumbnail. Add one large festive anchor plus several supporting elements: {scene} "
    "Use warm string lights generously. Clothing stays unchanged. At most one natural red Santa hat, on one "
    "adult only, never on a child, matching head shape, shadow and lighting. " + KEEP
)
WINTER_TEMPLATE = (
    "Turn this summer scene into a full Christmas winter scene while keeping it the same photo of the same "
    "product and the same people. Season change: bright overcast winter light, fresh snow on the ground and on "
    "outdoor surfaces, bare or snow-dusted trees and plants. Change the clothing to warm winter clothing that fits "
    "each person naturally (knit sweaters, jackets, beanies, scarves, boots), same body positions. Summer drinks "
    "and props that are not the product may become winter ones. Add Christmas: {scene} At most one natural red "
    "Santa hat, on one adult only, never on a child. " + KEEP
)
# Christmas in the sun (Jay, 27.9.2026): people in a pool or on a beach wearing coats in the snow is absurd.
# Water, a pool, a beach or swimwear keep the warm setting and the same clothes; the Christmas comes to them.
SUN_TEMPLATE = (
    "Create a rich, unmistakable Christmas-in-the-sun version of this exact image, one that reads as Christmas "
    "even as a small thumbnail. This is a warm-weather water scene, so the warm setting stays: the same sunshine, "
    "the same water, sand or pool deck, and the same swimwear and summer clothes on the same people. No snow, no "
    "frost, no winter clothing. Add a Christmas that belongs in the sun: {scene} Use warm string lights "
    "generously, with red and gold accents. At most one natural red Santa hat, on one adult only, never on a "
    "child, matching head shape, shadow and lighting. " + KEEP
)
TEMPLATES = {"scene": STRONG_TEMPLATE, "winter": WINTER_TEMPLATE, "sun": SUN_TEMPLATE}
# The code check behind the classifier's "sun" call: a winter pick whose picture holds water or swimwear.
WATER_WORDS = re.compile(r"\b(pools?|poolside|beach(es)?|bikinis?|swim\w*|ocean|sea|seaside|surf(ing|er|ers|board)?)\b", re.I)


NOT_LIFESTYLE = ("packshot", "packshot-tight", "variant", "main")
MAX_ASPECT = 3.0          # gpt-image-2 takes at most 3:1; a wider banner is padded to 3:1 first
ASPECT_TOL = 0.04         # an output within 4 percent of the original's aspect is kept as it is


# ---------------------------------------------------------------- helpers
def log(msg=""):
    print(msg, flush=True)


def asin_from(text):
    m = re.search(r"(?:/dp/|/gp/product/|/gp/aw/d/|/product/)([A-Z0-9]{10})", text)
    if m:
        return m.group(1)
    m = re.fullmatch(r"[A-Z0-9]{10}", text.strip())
    return m.group(0) if m else None


# ---------------------------------------------------------------- product references
# What people paste: a bare ASIN, a product link from any Amazon marketplace (with the
# tracking junk after it), or a share link from the app (amzn.to / a.co). One ASIN each.
AMAZON_HOST = re.compile(r"^(?:www\.|smile\.|m\.)?(amazon\.(?:com|ca|com\.mx|com\.br|co\.uk|de|fr|it|es|nl|se|pl|"
                         r"com\.be|ie|com\.tr|ae|sa|eg|in|co\.jp|sg|com\.au))$")
SHORT_HOSTS = {"amzn.to", "a.co", "amzn.eu", "amzn.asia"}
BARE_ASIN = re.compile(r"\b(?:B0[A-Z0-9]{8}|\d{9}[\dX])\b")
LINK = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:amazon\.[a-z.]{2,9}|amzn\.to|a\.co|amzn\.eu|amzn\.asia)/[^\s,;\"'<>)]+", re.I)


def resolve_short(url):
    """One HEAD request, no redirect follow: the first Location header already holds /dp/ASIN
    (measured on six amzn.to links, 26.9.2026). Returns the Location or ""."""
    r = subprocess.run(["curl", "-sI", "--max-time", "15", "-A", UA, url], capture_output=True)
    m = re.search(r"^location:\s*(\S+)", r.stdout.decode("utf-8", "ignore"), re.I | re.M)
    return m.group(1) if m else ""


def product_ref(text, resolve=True):
    """(asin, host) for one pasted item, or (None, reason)."""
    t = text.strip().strip("<>\"'")
    if re.fullmatch(r"[A-Z0-9]{10}", t):
        return t, "www.amazon.com"
    url = t if re.match(r"https?://", t, re.I) else "https://" + t
    m = re.match(r"https?://([^/?#]+)", url, re.I)
    host = m.group(1).lower() if m else ""
    if host in SHORT_HOSTS:
        if not resolve:
            return None, "short link that did not resolve"
        loc = resolve_short(url)
        return product_ref(loc, resolve=False) if loc else (None, "short link that did not resolve")
    hm = AMAZON_HOST.match(host)
    if not hm:
        return None, "not an Amazon link"
    if re.search(r"/stores/|/shop/|/s\?|[?&]me=", url):
        return None, "a store or search link, not a product"
    a = asin_from(url)
    return (a, "www." + hm.group(1)) if a else (None, "no ASIN in the link")


def refs_from_text(text, resolve=True):
    """Every product in a pasted blob or file, in order, deduplicated.
    Returns ([(asin, host)], [(item, reason)])."""
    found, bad, seen = [], [], set()
    spans = []
    for m in LINK.finditer(text):
        spans.append((m.start(), m.group(0)))
    covered = [(m.start(), m.end()) for m in LINK.finditer(text)]
    for m in re.finditer(r"https?://[^\s,;\"'<>)]+", text, re.I):   # any other link: reported, never mined
        if not any(a <= m.start() < b for a, b in covered):
            spans.append((m.start(), m.group(0)))
            covered.append((m.start(), m.end()))
    for m in BARE_ASIN.finditer(text):
        if not any(a <= m.start() < b for a, b in covered):
            spans.append((m.start(), m.group(0)))
    spans.sort()
    shorts = 0
    for _, item in spans:
        if resolve and any(h in item.lower() for h in SHORT_HOSTS) and shorts:
            time.sleep(0.5)
        shorts += any(h in item.lower() for h in SHORT_HOSTS)
        a, host = product_ref(item, resolve=resolve)
        if not a:
            bad.append((item, host))
        elif a not in seen:
            seen.add(a)
            found.append((a, host))
    return found, bad


def image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)


def read_plan(run):
    p = run / "plan.json"
    if not p.exists():
        sys.exit(f"plan.json missing in {run}. Run `list` first.")
    return json.loads(p.read_text())


def write_plan(run, plan):
    (run / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False))


def append_usage(run, entry):
    entry.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    with open(run / "usage.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_usage(run):
    p = run / "usage.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def originals_of(run):
    d = run / "originals"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))


def out_path(run, name):
    return run / "christmas" / (Path(name).stem + ".png")


# ---------------------------------------------------------------- fetch
# Codex runs commands in a sandbox with the network OFF by default. There curl fails with "could not
# resolve host" (6) or "could not connect" (7), which must not be mistaken for an Amazon captcha:
# a catalog run would mark every listing blocked and pause.
OFFLINE_CODES = {5, 6, 7}
_last_rc = [0]


def offline():
    return _last_rc[0] in OFFLINE_CODES


def say_offline():
    log("")
    log("NO INTERNET FROM HERE. This command reads Amazon, and the Codex sandbox has the network off.")
    log("Run the same command again with network access approved (Codex asks; answer yes), or start")
    log("Codex with network on for this folder. Nothing was marked blocked; nothing is lost.")


def curl(url, out=None, timeout=30):
    cmd = ["curl", "-sL", "--max-time", str(timeout), "-A", UA,
           "-H", "Accept-Language: en-US,en;q=0.9",
           "-H", "Accept: text/html,application/xhtml+xml,image/avif,image/webp,*/*;q=0.8",
           "--compressed", url]
    if out:
        cmd += ["-o", str(out)]
        r = subprocess.run(cmd, capture_output=True)
        _last_rc[0] = r.returncode
        return r.returncode == 0
    r = subprocess.run(cmd, capture_output=True)
    _last_rc[0] = r.returncode
    return r.stdout.decode("utf-8", "ignore") if r.returncode == 0 else ""


def parse_listing(page):
    title = ""
    m = re.search(r'id="productTitle"[^>]*>\s*(.*?)\s*<', page, re.S)
    if m:
        title = html.unescape(m.group(1)).strip()
    urls = []
    for key in ("hiRes", "large"):
        for u in re.findall(r'"%s"\s*:\s*"(https://[^"]+)"' % key, page):
            u = u.replace("\\/", "/")
            if ("images-amazon" in u or "media-amazon" in u) and u not in urls:
                urls.append(u)
        if urls:
            break
    # keep only product gallery images (m.media-amazon.com/images/I/...), drop video thumbs / sprites
    urls = [u for u in urls if "/images/I/" in u and not u.endswith(".gif")]
    return title, urls


def blocked(page):
    """Captcha, the Akamai interstitial (a 2 KB page with bm-verify), or an empty answer."""
    if not page or len(page) < 20000:
        return True
    low = page.lower()
    return ("api-services-support@amazon.com" in page) or ("type the characters you see" in low) or \
        ("bm-verify" in page) or ("_sec/verify" in page) or ("captcha" in low and "productTitle" not in page)


def fetch_listing(asin, run, keep_page=True, host="www.amazon.com"):
    """One attempt at the listing page, then the gallery. Returns (status, n_images):
    status is "ok", "blocked" (captcha or empty page), "few" (under 2 usable images) or "offline"
    (no network at all, the Codex sandbox default)."""
    originals = run / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    url = f"https://{host}/dp/{asin}"
    log(f"Fetching {url} (one attempt, no retries)")
    page = curl(url)
    if offline():
        return "offline", 0
    if keep_page:
        (run / "page.html").write_text(page)
    if blocked(page):
        return "blocked", 0
    title, urls = parse_listing(page)
    listing = {"asin": asin, "title": title, "source": url, "image_urls": urls}
    (run / "listing.json").write_text(json.dumps(listing, indent=2, ensure_ascii=False))
    log(f"Title: {title[:90]}")
    log(f"Found {len(urls)} image URLs")
    if len(urls) < 2:
        return "few", len(urls)
    ok = 0
    for i, u in enumerate(urls, 1):
        ext = ".png" if u.lower().endswith(".png") else ".jpg"
        dest = originals / f"{i:02d}{ext}"
        if dest.exists() and dest.stat().st_size > 5000:
            ok += 1
            continue
        good = curl(u, dest, timeout=60) and dest.exists() and dest.stat().st_size > 5000
        if good:
            ok += 1
            log(f"  {dest.name}  {dest.stat().st_size // 1024} KB")
        else:
            log(f"  {dest.name}  FAILED (not retrying)")
            if dest.exists():
                dest.unlink()
    log(f"Downloaded {ok}/{len(urls)} to {originals}")
    return ("ok" if ok >= 2 else "few"), ok


def cmd_fetch(args):
    src = args.source
    out = Path(args.out) if args.out else None
    src_path = Path(os.path.expanduser(src))
    if src_path.is_dir():
        run = out or Path.cwd() / "santa" / src_path.name
        originals = run / "originals"
        originals.mkdir(parents=True, exist_ok=True)
        n = 0
        for p in sorted(src_path.iterdir()):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                shutil.copy(p, originals / p.name)
                n += 1
        listing = {"asin": src_path.name, "title": args.title or src_path.name,
                   "source": "folder", "folder": str(src_path), "images": n}
        (run / "listing.json").write_text(json.dumps(listing, indent=2))
        log(f"Copied {n} images from {src_path} to {originals}")
        if n < 2:
            sys.exit("Fewer than 2 images. Nothing to transform beyond a main image.")
        log(f"Next: python3 {SELF} next {run}")
        return

    asin, host = product_ref(src)
    if not asin:
        sys.exit(f"Not an ASIN, an Amazon product link or a folder ({host}): {src}")
    run = out or Path.cwd() / "santa" / asin
    status, n = fetch_listing(asin, run, host=host)
    if status == "offline":
        say_offline()
        sys.exit(3)
    if status == "blocked":
        log("")
        log("AMAZON FETCH BLOCKED OR EMPTY.")
        log("Amazon returned a captcha or nothing. This skill will not retry (retries get IPs blocked).")
        log("Fallback: save the listing images to a folder and re-run with that folder:")
        log(f"  python3 {SELF} fetch /path/to/images --out {run}")
        sys.exit(2)
    if status == "few":
        log("Fewer than 2 gallery images found. Use the folder fallback:")
        log(f"  python3 {SELF} fetch /path/to/images --out {run}")
        sys.exit(2)
    log(f"Next: python3 {SELF} next {run}")


# ---------------------------------------------------------------- list
def ensure_plan(run):
    """Write the plan.json template if it is missing. Returns the plan."""
    if (run / "plan.json").exists():
        return read_plan(run)
    listing = {}
    lp = run / "listing.json"
    if lp.exists():
        listing = json.loads(lp.read_text())
    plan = {"asin": listing.get("asin", run.name), "title": listing.get("title", ""),
            "images": [{"file": p.name, "role": "TODO", "action": "TODO", "scene": "", "reason": ""}
                       for p in originals_of(run)]}
    write_plan(run, plan)
    return plan


def cmd_list(args):
    run = Path(args.run)
    for p in originals_of(run):
        w, h = image_size(p)
        log(f"{p.name}  {w}x{h}  aspect {w / max(h, 1):.2f}")
    ensure_plan(run)
    log(f"\nplan.json is ready. Next: python3 {SELF} next {run}")


# ---------------------------------------------------------------- classify (Codex looks)
# The same decision rules the Claude Code build sends to gpt-5.4, word for word, with the image
# numbering changed: there the strip was "image 2" of the API call; here Codex opens it by path.
CLASSIFY_RULES = """You classify each Amazon product listing image below for a Christmas-edition workflow.
Product title: "{title}"
The gallery has {total} images. Image 01 is the main image and is never transformed.

Decide role, action, and (for transforms) a scene line for gpt-image-2. Go through these in order:
1. Two or more separate photos tiled in one frame (panels, split screen, grid), or two or more color
   versions of the product side by side? role collage, SKIP.
2. The subject is a brand logo card, a retail box on its own, or a brand graphic (a logo and a claim on a
   flat colored background, the product only a cut-out decoration at the edge)? role logo, SKIP.
3. The gallery strip shows the whole gallery numbered, for context only; judge the image itself. If it is
   the product on plain white in a DIFFERENT color or finish than the main image (01 in the strip), it is
   the main image of a sibling listing: role variant, SKIP, reason "color variant".
3b. Any other image of the product on a plain white or plain studio background, with or without
   props, labels or badges: role packshot, SKIP, reason "product on white". Santa dresses lifestyle
   images only.
4. Count the separate text elements (a headline and its sub-line count as two). Text in a colored band
   above or below the photo, or on a plain area beside it, is NOT on the product. It is an infographic
   ONLY if one of these is true: five or more text elements; feature bullets, spec tables, dimension
   arrows, diagrams or comparison charts; or text printed across the product's own body. Then role
   infographic, SKIP, and the reason says which of the three.
5. Everything else is a lifestyle photo that can take Christmas: the product in a real setting, people
   or not, with or without a headline band. role lifestyle, TRANSFORM. A headline plus a
   sub-line is the normal Amazon lifestyle frame; gpt-image-2 keeps short text intact and a later pass
   reads it back. When you hesitate between lifestyle and infographic, choose lifestyle.
- mode, for every transform: "sun" when people are in the water, at a pool or on a beach, or in swimwear
  (a pool, a beach, a swim spot, even with nobody in the water): snow and coats there are absurd, so the
  warm setting and the clothes stay and Christmas comes in the sun. "sun" wins over "winter". "winter"
  for any other summer or warm-weather scene (outdoors in sun, a patio, a backyard, people in summer
  clothes), which becomes a full winter scene with snow and winter clothing; "scene" for everything else.
  Set summer=true with "winter" only.
- Scene line: what gets added and WHERE, anchored to what is IN this picture. The treatment is RICH:
  it must read as Christmas at thumbnail size.
  "scene": ONE large anchor plus 3 or 4 supporting elements. Anchor: a tall, fully decorated Christmas
  tree with warm lights in any home interior; elsewhere a big lit wreath, a lit garland along a shelf,
  railing or fence, or a small decorated evergreen outdoors. Supporting: lit garland, wrapped gift boxes
  on the floor or a far surface, stockings on the wall, a wreath, warm string lights, snow falling outside
  a window, a light dusting of snow on outdoor ground. Name where each one goes (back right beside the
  window, on the floor to the far left). Never on, in front of or touching the product or anyone's hands.
  "winter": the Christmas additions (lights along the fence, a small decorated evergreen in the corner,
  gifts on the far side) plus anything product-specific that must stay ("Keep the drinks on the cooler
  lid").
  "sun": a Christmas that belongs in the heat: warm string lights along the umbrella, railing or fence, a
  small decorated palm or Christmas tree on the deck or sand, tinsel, wrapped gifts on the far side, red and
  gold accents. Never snow, never winter clothing.
  Santa hat: name the adult who gets it ("One natural red Santa hat on the father only"), or write
  "No Santa hat (children only)" when only children appear. Never more than one.
  Say what must stay exactly ("Keep the dog and the gray dog bed exactly as they are, nothing on the bed.").
  If the image carries a headline or labels, START with: Keep the headline '...' and the labels exactly as
  written, same position.
  Good: "A fully decorated Christmas tree with warm lights clearly visible behind the sofa on the right, a
  lit garland with bows along the windowsill, three wrapped gift boxes on the floor to the far left, and
  snow falling outside the window. Keep the dog and the gray dog bed exactly as they are, nothing on the bed."
  Good: "Warm string lights along the whole edge of the orange tent and between the trees above, a large
  evergreen wreath with a red bow on the tent door, three wrapped gifts beside the tent, a light dusting of
  snow on the ground. One natural red Santa hat on the father only. Keep the tent orange."
  Bad: "make it Christmassy" (no anchor), "add a red glow" (a filter), "put a bow on it" (touches the
  product), "Santa hats on everyone".
- seen: one line saying what is in the picture (written from the picture, not the file name).
- reason: for skips, why, in five words or fewer."""

CLASSIFY_SHAPE = ('{"02.jpg": {"seen": "...", "role": "...", "action": "transform|skip", "mode": "scene|winter|sun", '
                  '"scene": "...", "reason": "...", "summer": false, "text_on_image": "the headline/labels you can read, or empty"}, '
                  '"03.jpg": {...}}')


def gallery_strip(run, px=150):
    """All originals in one numbered strip, so the looker knows the gallery (colour variants)."""
    from PIL import Image, ImageDraw
    files = originals_of(run)
    cols = min(len(files), 8)
    rows = (len(files) + cols - 1) // cols
    strip = Image.new("RGB", (cols * px, rows * (px + 16)), "white")
    dr = ImageDraw.Draw(strip)
    for k, f in enumerate(files):
        with Image.open(f) as im:
            im = im.convert("RGB")
            im.thumbnail((px - 6, px - 6))
            x, y = (k % cols) * px, (k // cols) * (px + 16)
            strip.paste(im, (x + 3, y + 16))
        dr.text((x + 4, y + 2), f.stem, fill="black")
    path = run / "gallery-strip.jpg"
    strip.save(path, quality=80)
    return path


def classify_todo(run, force=False):
    plan = ensure_plan(run)
    imgs = plan["images"]
    changed = False
    if imgs and imgs[0].get("action") in ("TODO", "", None):
        imgs[0].update({"seen": "main image (first in gallery)", "role": "main", "action": "skip",
                        "scene": "", "reason": "main image stays white", "summer": False})
        changed = True
    if changed:
        write_plan(run, plan)
    return plan, [img for img in imgs[1:] if force or img.get("action") in ("TODO", "", None)]


def print_classify_task(run, force=False):
    """The looking task for Codex. Returns the number of images to look at."""
    plan, todo = classify_todo(run, force)
    if not todo:
        return 0
    strip = gallery_strip(run)
    run = run.resolve()
    log(f"TASK classify  {run}")
    log(f"Product: {plan.get('title') or plan.get('asin')}")
    log("")
    log("1. Open the whole gallery first (view_image):")
    log(f"     {strip.resolve()}")
    log("2. Open each image below with view_image and decide it by the rules that follow:")
    for img in todo:
        log(f"     {img['file']}   {(run / 'originals' / img['file'])}")
    log("")
    log(CLASSIFY_RULES.format(title=plan.get("title", ""), total=len(plan["images"])))
    log("")
    log(f"3. Write your answers to {run / 'classify.json'} as one JSON object keyed by file name:")
    log(f"     {CLASSIFY_SHAPE}")
    log(f"4. Run: python3 {SELF} classify {run} --apply")
    return len(todo)


def apply_classify(run, path=None):
    run = Path(run)
    path = Path(path) if path else run / "classify.json"
    if not path.exists():
        sys.exit(f"{path} not found. Write the answers there first.")
    try:
        answers = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON ({e}). Fix it and run --apply again.")
    plan = ensure_plan(run)
    imgs = plan["images"]
    n = 0
    for i, img in enumerate(imgs):
        d = answers.get(img["file"]) or answers.get(Path(img["file"]).stem)
        if i == 0 or not isinstance(d, dict) or img.get("role") == "duplicate":
            continue
        n += 1
        action = d.get("action", "skip")
        if action not in ("transform", "skip"):
            action = "skip"
        img.update({"seen": d.get("seen", ""), "role": d.get("role", ""), "action": action,
                    "scene": d.get("scene", "") if action == "transform" else "",
                    "reason": d.get("reason", "") if action == "skip" else "",
                    "summer": bool(d.get("summer")), "text_on_image": d.get("text_on_image", ""),
                    "mode": d.get("mode", "")})
        if action == "transform":
            img["mode"] = prompt_mode(img if img["mode"] in TEMPLATES else {**img, "mode": ""})
            if img["mode"] == "winter" and WATER_WORDS.search(img["seen"]):
                # a pool, a beach or swimwear never gets snow and coats (Jay, 27.9.2026). Enforced here
                # too, and the winter line's snow is dropped so it cannot argue with the sun template.
                img.update({"mode": "sun", "summer": False,
                            "scene": re.sub(r"(,\s*(and\s+)?|\s+and\s+)?[^,.]*\bsnow\w*[^,.]*", "", img["scene"])})
        else:
            img["mode"] = ""
        if action == "transform" and not img["scene"].strip():
            img.update({"action": "skip", "reason": "no scene line"})
        if img["action"] == "transform" and img["role"] in NOT_LIFESTYLE:
            # Santa dresses lifestyle images only (Jay, 26.9.2026): never the product on white,
            # never the main image, never an infographic. Enforced here, not left to the model.
            img.update({"action": "skip", "scene": "", "mode": "", "reason": "product on white"})
        log(f"  {img['file']}  {img['role']:<14} {img['action']:<9} {img.get('mode', ''):<8} {img.get('reason') or img['scene'][:60]}")
    write_plan(run, plan)
    missing = [img["file"] for img in imgs if img.get("action") in ("TODO", "", None)]
    n_t = sum(1 for img in imgs if img.get("action") == "transform")
    log(f"{n} answers applied. {n_t} to transform, {len(imgs) - n_t} skipped.")
    if missing:
        log(f"Still unanswered: {', '.join(missing)}. Add them to {path.name} and run --apply again.")
    append_usage(run, {"op": "classify", "images": n})


def cmd_classify(args):
    run = Path(args.run)
    if args.apply is not None:
        apply_classify(run, args.apply or None)
    elif not print_classify_task(run, args.force):
        log("plan.json already classified (use --force to redo).")


# ---------------------------------------------------------------- generate (Codex's image_gen)
def prompt_mode(img):
    if img.get("mode") in TEMPLATES:
        return img["mode"]
    return "winter" if img.get("summer") else "scene"


def build_prompt(img):
    scene = img["scene"].strip()
    lock = "Studio packshot. Keep the camera exactly where it is"
    if scene.startswith(lock):   # plans written before 26.9 carried the lock in the scene line
        scene = scene.split("The background stays a bright white studio.", 1)[-1].strip()
    prompt = TEMPLATES[prompt_mode(img)].format(scene=scene)
    if re.search(r"\bno Santa hats?\b", scene, re.I):
        prompt = re.sub(r"At most one natural red Santa hat.*?\. ", "No Santa hats. ", prompt)
    return prompt


def _median_edge(strips):
    """Per-channel median of the border strips (no numpy needed)."""
    out = []
    for k in range(3):
        hist = [0] * 256
        for st in strips:
            for i, n in enumerate(st.split()[k].histogram()):
                hist[i] += n
        half, acc = sum(hist) / 2, 0
        for i, n in enumerate(hist):
            acc += n
            if acc >= half:
                out.append(i)
                break
    return tuple(out)


def write_input(src, dest):
    """The image Codex opens with view_image before image_gen: a PNG of the original. The built-in
    tool picks its own output size and follows the input's shape (outputs on this Mac came back at
    1958x803, 816x1927, 1254x1254). gpt-image-2 takes at most 3:1, so a wider or taller original is
    padded to 3:1 with its own border colour and cropped back in `place`. Returns the box
    (x, y, w, h) of the original inside the input, as fractions of the input."""
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        r = w / h
        if 1 / MAX_ASPECT <= r <= MAX_ASPECT:
            im.save(dest, "PNG")
            return [0.0, 0.0, 1.0, 1.0]
        # grow the short side until the canvas is exactly 3:1 (or 1:3); never shrink the original
        W, H = (w, round(w / MAX_ASPECT)) if r > MAX_ASPECT else (round(h / MAX_ASPECT), h)
        edges = [im.crop(b) for b in ((0, 0, w, 2), (0, h - 2, w, h), (0, 0, 2, h), (w - 2, 0, w, h))]
        canvas = Image.new("RGB", (W, H), _median_edge(edges))
        x, y = (W - w) // 2, (H - h) // 2
        canvas.paste(im, (x, y))
        canvas.save(dest, "PNG")
        return [x / W, y / H, w / W, h / H]


def pending_jobs(run, only=None):
    """Every transform in plan.json that has no output yet and was not refused."""
    plan = read_plan(run)
    jobs = []
    for img in plan["images"]:
        if img.get("action") != "transform":
            continue
        if only and img["file"] != only:
            continue
        if out_path(run, img["file"]).exists() or img.get("gen_failed"):
            continue
        if not img.get("scene"):
            continue
        if img.get("role") in NOT_LIFESTYLE:
            continue
        jobs.append(img)
    return jobs


def prepare_jobs(run, only=None, limit=None):
    """Write the inputs and jobs.json for pending transforms. Returns the job list."""
    run = Path(run)
    todo = pending_jobs(run, only)
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        return []
    (run / "christmas" / "_input").mkdir(parents=True, exist_ok=True)
    jobs_path = run / "jobs.json"
    old = json.loads(jobs_path.read_text()) if jobs_path.exists() else {}
    now = time.time()
    jobs = []
    for img in todo:
        src = run / "originals" / img["file"]
        inp = run / "christmas" / "_input" / (Path(img["file"]).stem + ".png")
        box = write_input(src, inp)
        job = {"file": img["file"], "input": str(inp.resolve()), "box": box,
               "prompt": build_prompt(img), "prepared_at": now}
        previous = old.get(img["file"], {})
        if all(previous.get(k) == job[k] for k in ("input", "box", "prompt")):
            job["prepared_at"] = previous["prepared_at"]
        old[img["file"]] = job
        jobs.append(job)
    jobs_path.write_text(json.dumps(old, indent=2, ensure_ascii=False))
    return jobs


def print_generate_task(run, jobs):
    run = Path(run).resolve()
    log(f"TASK generate  {run}   {len(jobs)} image(s)")
    log("")
    log("Use your built-in image_gen tool, on the ChatGPT plan. Never the imagegen CLI fallback, never an")
    log("API key, never another image model. One image_gen call per job, one at a time, no retries.")
    log("For EACH job below, in order:")
    log("  a. view_image the INPUT, so it is in the conversation.")
    log("  b. image_gen: EDIT that image (the edit target is the input you just opened) with the PROMPT,")
    log("     Set referenced_image_paths=[INPUT] explicitly; send the PROMPT exactly as written.")
    log(f"  c. python3 {SELF} place {run} <FILE> --latest")
    log("     (or pass the saved image's path instead of --latest if image_gen told you where it is)")
    log(f"  If image_gen refuses or errors: python3 {SELF} place {run} <FILE> --failed \"<why, few words>\"")
    log("  and go on to the next job. Do not try again. On a plan usage limit, STOP without --failed;")
    log("  leave the pending job intact so continue santa resumes it.")
    for j in jobs:
        log("")
        log(f"--- FILE {j['file']}")
        log(f"INPUT  {j['input']}")
        log(f"PROMPT {j['prompt']}")


def cmd_prepare(args):
    run = Path(args.run)
    jobs = prepare_jobs(run, only=args.only)
    if not jobs:
        log("Nothing to generate.")
        return
    print_generate_task(run, jobs)


def codex_images_dir():
    return Path(os.path.expanduser(os.getenv("CODEX_HOME", "~/.codex"))) / "generated_images"


def latest_generated(after, used):
    root = codex_images_dir()
    if not root.exists():
        return None
    best = None
    for p in root.rglob("*"):
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or str(p) in used:
            continue
        m = p.stat().st_mtime
        if m >= after - 1 and (best is None or m > best[0]):
            best = (m, p)
    return best[1] if best else None


def fit_output(gen, box, orig_size):
    """image_gen's output back to the original's frame. Returns (PIL image, note)."""
    from PIL import Image
    im = Image.open(gen).convert("RGB")
    W, H = im.size
    ow, oh = orig_size
    target = ow / oh
    bx, by, bw, bh = box
    if box != [0.0, 0.0, 1.0, 1.0]:
        in_aspect = target * 1 / bw * bh    # aspect of the padded input
        if abs((W / H) / in_aspect - 1) <= ASPECT_TOL:
            im = im.crop((round(bx * W), round(by * H), round((bx + bw) * W), round((by + bh) * H)))
            W, H = im.size
    if abs((W / H) / target - 1) <= ASPECT_TOL:
        return im, ""
    # the model changed the shape: centre-crop to the original's aspect; the framing check measures the rest
    if W / H > target:
        nw = round(H * target)
        im = im.crop(((W - nw) // 2, 0, (W - nw) // 2 + nw, H))
    else:
        nh = round(W / target)
        im = im.crop((0, (H - nh) // 2, W, (H - nh) // 2 + nh))
    return im, f"image_gen returned {W}x{H} for a {ow}x{oh} original; centre-cropped to the original's shape"


def cmd_place(args):
    run = Path(args.run)
    plan = read_plan(run)
    img = next((i for i in plan["images"] if i["file"] == args.name), None)
    if not img:
        sys.exit(f"{args.name} not in plan.json")
    jobs = json.loads((run / "jobs.json").read_text()) if (run / "jobs.json").exists() else {}
    job = jobs.get(args.name)
    if not job:
        sys.exit(f"No prepared job for {args.name}. Run `next` (or `prepare`) first.")
    if args.failed:
        img["gen_failed"] = args.failed
        attempt1 = run / "christmas" / (Path(args.name).stem + ".attempt1.png")
        if img.get("regenerated") and attempt1.exists() and not out_path(run, args.name).exists():
            # the retry was refused: the first attempt comes back, reported as not stage ready
            shutil.move(attempt1, out_path(run, args.name))
            img["stage_ready"] = False
            img["verdict"] = f"Regeneration refused ({args.failed}); first attempt kept. " + img.get("prev_verdict", "")
            img["checks"] = img.get("prev_checks", {})
        write_plan(run, plan)
        append_usage(run, {"op": "edit", "file": args.name, "failed": args.failed})
        log(f"{args.name}: recorded as not generated ({args.failed}). No retry.")
        return
    used = {u.get("source") for u in read_usage(run) if u.get("source")}
    if args.path and args.path != "--latest":
        gen = Path(os.path.expanduser(args.path))
    else:
        gen = latest_generated(job["prepared_at"], used)
        if gen is None:
            sys.exit(f"No new image found in {codex_images_dir()} since this job was prepared. "
                     f"Pass the image's path: python3 {SELF} place {run} {args.name} /path/to/image.png")
    if not gen.exists():
        sys.exit(f"{gen} does not exist.")
    src = run / "originals" / args.name
    im, note = fit_output(gen, job["box"], image_size(src))
    dest = out_path(run, args.name)
    dest.parent.mkdir(exist_ok=True)
    im.save(dest, "PNG")
    img.pop("gen_failed", None)
    if note:
        img["shape_note"] = note
    write_plan(run, plan)
    append_usage(run, {"op": "edit", "file": args.name, "source": str(gen), "size": f"{im.width}x{im.height}",
                       "regen": bool(img.get("regenerated"))})
    small = "  (longest side under 1000 px: upscale before upload)" if max(im.size) < 1000 else ""
    log(f"{args.name} -> {dest}  {im.width}x{im.height}{small}" + (f"\n  note: {note}" if note else ""))


def prepare_regen(run, name, note=None):
    """Apply the one-retry rule and move the first attempt aside. Returns True if a retry is allowed."""
    plan = read_plan(run)
    hit = next((img for img in plan["images"] if img["file"] == name), None)
    if not hit:
        log(f"{name} not in plan.json")
        return False
    if hit.get("regenerated"):
        log(f"{name} was already regenerated once. The rule is one retry. Report it instead.")
        return False
    note = note or hit.get("regen_note")
    if note:
        hit["scene"] = hit["scene"].rstrip(". ") + ". " + note.strip()
    hit["regenerated"] = True
    hit["prev_verdict"] = hit.get("verdict", "")
    hit["prev_checks"] = hit.get("checks", {})
    for k in ("verdict", "stage_ready", "checks", "gen_failed", "shape_note"):
        hit.pop(k, None)
    write_plan(run, plan)
    dest = out_path(run, name)
    if dest.exists():
        shutil.move(dest, run / "christmas" / (Path(name).stem + ".attempt1.png"))
    return True


def cmd_regen(args):
    run = Path(args.run)
    if not prepare_regen(run, args.name, args.note):
        sys.exit(1)
    jobs = prepare_jobs(run, only=args.name)
    print_generate_task(run, jobs)


# ---------------------------------------------------------------- check
def mean_luma(path):
    from PIL import Image, ImageStat
    with Image.open(path) as im:
        im = im.convert("L")
        im.thumbnail((256, 256))
        return ImageStat.Stat(im).mean[0]


def brightness_delta(src, dest):
    a, b = mean_luma(src), mean_luma(dest)
    return a, b, (b - a) / max(a, 1) * 100


def cmd_check(args):
    """Numbers to back the eye pass: mean brightness before vs after. A drop over 8 percent is a flag."""
    run = Path(args.run)
    plan = read_plan(run)
    flagged = 0
    for img in plan["images"]:
        if img.get("action") != "transform":
            continue
        src = run / "originals" / img["file"]
        dest = out_path(run, img["file"])
        if not dest.exists():
            log(f"{img['file']}: no output")
            continue
        a, b, delta = brightness_delta(src, dest)
        flag = "  <-- DARKER, check it" if delta < -8 else ""
        flagged += bool(flag)
        log(f"{img['file']}: brightness {a:.0f} -> {b:.0f} ({delta:+.0f}%){flag}")
    log(f"{flagged} flagged. The eye pass (product shape, props, text) is still yours.")


# ---------------------------------------------------------------- verify (Codex judges, the script measures)
VERIFY_RULES = """You are the quality gate for a Christmas-edition Amazon listing image.
For each pair, A is the ORIGINAL and B is the generated Christmas version. The product is the thing
being sold; Christmas may happen AROUND it, never TO it. Each pair lists the text that was on the
original, what the original shows (from the classifier) and its mode.

The treatment is meant to be RICH, so do not fail an image for having a lot of Christmas: a large
anchor (a lit tree in any home interior, a big wreath, lit garlands) plus several gifts, stockings and
string lights is correct. In mode "winter" a summer scene becomes full winter: snow outdoors and warm
winter clothing on the same people in the same poses is correct. In mode "sun", retain sunshine, water, sand and swimwear; no snow, frost or winter clothing.

Answer each question about B compared to A:
1. same_product: identical shape, color, angle, size, label text, packaging? (drift = false)
2. nothing_touching: nothing ADDED on, in front of, or touching the product, and the product is not
   hidden? (Things that were already on it in A, like a drink on a cooler lid, stay and are fine.)
3. not_darker: B is as bright as A or brighter overall, no moody/dim look? (warmer is fine; a bright
   overcast winter sky is fine)
4. no_tint: no red/green filter over the whole image?
5. decor_ok: every added thing rests on a real surface or hangs naturally (a wall, a window, a fence, a
   tree), nothing floating?
6. people_ok: same faces, identity, poses, hands and expressions; clothing unchanged except, in mode
   "winter", summer clothes turned into winter clothes; at most one Santa hat (the red hat with white
   fur trim and a pompom), on an adult, never a child? A knit beanie or any other winter hat is
   clothing, not a Santa hat.
7. text_ok: every headline and label from A still present and spelled the same, no invented text or logos?
8. no_banned: no glitter, no cartoon items, no sparkle or snow overlay pasted flat over the photo; in mode "sun", no snow or frost anywhere?
9. christmas_obvious: would a shopper see "Christmas" at a glance at thumbnail size?
10. same_framing: compare actual product boundaries and camera framing side by side; same size and position?
stage_ready is true only if ALL ten are true. Write a one or two sentence verdict saying what
changed and what, if anything, drifted. If not ready, write regen_note: one sentence of instruction
for the second attempt that names the failure (e.g. "The saw must stay blue. Add a lit tree behind
the sofa so it reads as Christmas.").

The script's scale and shift are scene-centre template-match estimates, not product segmentation.
They can match the background after a season change. Treat them as advisory and explicitly check
same_framing visually. A low score or missing OpenCV is NOT a pass. Read the small text on the
product's own label at full size; a changed digit is a same_product or text_ok failure."""

VERIFY_KEYS = ("same_product", "nothing_touching", "not_darker", "no_tint", "decor_ok",
               "people_ok", "text_ok", "no_banned", "christmas_obvious")
VERIFY_SHAPE = ('{"02.jpg": {"same_product": true, "nothing_touching": true, "not_darker": true, "no_tint": true, '
                '"decor_ok": true, "people_ok": true, "text_ok": true, "no_banned": true, "christmas_obvious": true, '
                '"same_framing": true, "verdict": "...", "regen_note": ""}, "03.jpg": {...}}')

FRAMING_MIN_SCALE = 0.88   # measured 26.9.2026: shrunk frames read 0.62-0.80, intact ones 0.97-1.0
FRAMING_MAX_SHIFT = 0.12   # centre moved by more than 12 percent of the width


def framing_check(orig, out, W=320):
    """Find the original's product (or centre) inside the output at 55-120 percent scale.
    Returns {"scale", "shift", "score"} or None when OpenCV is missing. The model cannot judge
    size: gpt-5.4 passed two of three frames whose product had shrunk by a quarter."""
    try:
        import numpy as np
        import cv2
        from PIL import Image
    except ImportError:
        return None
    a = np.array(Image.open(orig).convert("L"))
    b = np.array(Image.open(out).convert("L"))
    h = round(a.shape[0] * W / a.shape[1])
    a = cv2.resize(a, (W, h), interpolation=cv2.INTER_AREA)
    b = cv2.resize(b, (W, h), interpolation=cv2.INTER_AREA)  # same frame after place()
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    ys, xs = np.where(np.abs(a.astype(int) - np.median(border)) > 25)
    if len(xs) and (xs.max() - xs.min()) * (ys.max() - ys.min()) < 0.85 * W * h:
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()       # a product on a plain ground
    else:
        x0, x1, y0, y1 = int(W * .2), int(W * .8), int(h * .2), int(h * .8)  # a full scene: its centre
    t = a[y0:y1 + 1, x0:x1 + 1]
    best = (-1.0, 1.0, 0.0, 0.0)
    for s in np.arange(0.55, 1.21, 0.025):
        tw, th = round(t.shape[1] * s), round(t.shape[0] * s)
        if tw < 12 or th < 12 or tw > W or th > h:
            continue
        r = cv2.matchTemplate(b, cv2.resize(t, (tw, th)), cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx > best[0]:
            best = (mx, s, loc[0] + tw / 2, loc[1] + th / 2)
    score, s, cx, cy = best
    shift = ((cx - (x0 + x1) / 2) ** 2 + (cy - (y0 + y1) / 2) ** 2) ** .5 / W
    return {"scale": round(float(s), 3), "shift": round(float(shift), 3), "score": round(float(score), 2)}


def framing_moved(fr):
    return fr is not None and fr["score"] >= 0.5 and (fr["scale"] < FRAMING_MIN_SCALE or fr["shift"] > FRAMING_MAX_SHIFT)


def verify_todo(run, only=None, force=False):
    plan = read_plan(run)
    out = []
    for img in plan["images"]:
        if img.get("action") != "transform" or (only and img["file"] != only):
            continue
        if img.get("duplicate_of") or not out_path(run, img["file"]).exists():
            continue
        if "stage_ready" in img and not force and not only:
            continue
        out.append(img)
    return out


def print_verify_task(run, only=None, force=False):
    todo = verify_todo(run, only, force)
    if not todo:
        return 0
    run = Path(run).resolve()
    log(f"TASK verify  {run}   {len(todo)} pair(s)")
    log("")
    log("Open A then B of every pair with view_image and answer the ten questions below. The script")
    log("has already measured size, position and brightness; they are printed with each pair.")
    for img in todo:
        src = run / "originals" / img["file"]
        dest = out_path(run, img["file"])
        a, b, delta = brightness_delta(src, dest)
        fr = framing_check(src, dest)
        meas = f"brightness {a:.0f} -> {b:.0f} ({delta:+.0f}%)" + ("  <-- DARKER, look" if delta < -8 else "")
        if fr is None:
            meas += "; framing not measured (opencv missing)"
        else:
            meas += f"; scene-match scale {fr['scale']}, shift {fr['shift']}, match {fr['score']}"
            meas += "  <-- ADVISORY: check actual product boundaries" if framing_moved(fr) else ""
        log("")
        log(f"--- FILE {img['file']}   mode {prompt_mode(img)}")
        log(f"A  {src}")
        log(f"B  {dest}")
        log(f"text on the original: \"{img.get('text_on_image', '')}\"")
        log(f"what the original shows: \"{img.get('seen', '')}\"")
        log(f"measured: {meas}")
    log("")
    log(VERIFY_RULES)
    log("")
    log(f"Write your answers to {run / 'verify.json'} as one JSON object keyed by file name:")
    log(f"  {VERIFY_SHAPE}")
    log(f"Then run: python3 {SELF} verify {run} --apply")
    return len(todo)


def apply_verify(run, path=None):
    """Merge Codex's answers with the measured framing. Returns the list of failing files."""
    run = Path(run)
    path = Path(path) if path else run / "verify.json"
    if not path.exists():
        sys.exit(f"{path} not found. Write the answers there first.")
    try:
        answers = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON ({e}). Fix it and run --apply again.")
    plan = read_plan(run)
    failing, n = [], 0
    for img in plan["images"]:
        d = answers.get(img["file"]) or answers.get(Path(img["file"]).stem)
        if img.get("action") != "transform" or not isinstance(d, dict):
            continue
        dest = out_path(run, img["file"])
        if not dest.exists():
            continue
        n += 1
        checks = {k: bool(d.get(k, False)) for k in VERIFY_KEYS}
        verdict = d.get("verdict", "")
        note = d.get("regen_note", "")
        fr = framing_check(run / "originals" / img["file"], dest)
        # Template matching measures the scene centre, not a segmented product. It may
        # match changed scenery (observed in the live winter test); retain it as advisory.
        img["framing"] = fr
        checks["same_framing"] = d.get("same_framing") is True
        if "same_framing" not in d:
            verdict = "Framing has not been visually verified. " + verdict
        if framing_moved(fr):
            img["framing_warning"] = "Scene-centre matching suggests movement; inspect the actual product boundaries."
        else:
            img.pop("framing_warning", None)
        ready = all(checks.values())
        img.update({"stage_ready": ready, "verdict": verdict, "checks": checks})
        if not ready:
            img["regen_note"] = note
            failing.append(img["file"])
        log(f"  {img['file']}  {'READY' if ready else 'NOT READY'}  {verdict[:100]}")
    write_plan(run, plan)
    append_usage(run, {"op": "verify", "pairs": n})
    log(f"{n} verdicts applied. {len(failing)} not stage ready: {', '.join(failing) if failing else 'none'}")
    return failing


def cmd_verify(args):
    run = Path(args.run)
    if args.apply is not None:
        apply_verify(run, args.apply or None)
    elif not print_verify_task(run, args.only, args.force):
        log("Nothing to verify.")


# ---------------------------------------------------------------- next: the loop Codex runs
def next_step(run, allow_generate=True):
    """What this listing needs next: classify | generate | verify | regen | finished."""
    plan = ensure_plan(run)
    if any(img.get("action") in ("TODO", "", None) for img in plan["images"]):
        return "classify", None
    if allow_generate and pending_jobs(run):
        return "generate", None
    if verify_todo(run):
        return "verify", None
    for img in plan["images"]:
        if img.get("stage_ready") is False and not img.get("regenerated") and allow_generate:
            return "regen", img["file"]
    return "finished", None


def do_next(run, allow_generate=True, limit=None):
    """Print the task for the next step. Returns the step name."""
    run = Path(run)
    step, name = next_step(run, allow_generate)
    if step == "classify":
        print_classify_task(run)
    elif step == "generate":
        print_generate_task(run, prepare_jobs(run, limit=limit))
    elif step == "verify":
        print_verify_task(run)
    elif step == "regen":
        prepare_regen(run, name)
        log(f"{name} failed the gate. Its one regeneration, with the failure written into the scene:")
        print_generate_task(run, prepare_jobs(run, only=name))
    return step


def cmd_next(args):
    run = Path(args.run)
    if not originals_of(run):
        sys.exit(f"No originals in {run}. Run fetch first.")
    step = do_next(run)
    if step == "finished":
        write_sheet(run)
        plan = read_plan(run)
        t = [i for i in plan["images"] if i.get("action") == "transform"]
        ready = sum(1 for i in t if i.get("stage_ready"))
        log("DONE")
        log(f"{len(t)} transformed ({ready} stage ready), {len(plan['images']) - len(t)} skipped.")
        log(f"Open: {(run / 'contact-sheet.html').resolve()}")
    else:
        log("")
        log(f"When this task is done, run: python3 {SELF} next {run.resolve()}")


# ---------------------------------------------------------------- sheet + report
def thumb_b64(path, max_px=900):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def images_made(run):
    return [u for u in read_usage(run) if u.get("op") == "edit" and u.get("source")]


def write_sheet(run):
    plan = read_plan(run)
    made = images_made(run)
    title = plan.get("title") or run.name
    asin = plan.get("asin", run.name)
    rows = []
    for img in plan["images"]:
        src = run / "originals" / img["file"]
        dest = out_path(run, img["file"])
        b = thumb_b64(src)
        if img.get("action") == "transform" and dest.exists():
            a = thumb_b64(dest)
            after = f'<a href="christmas/{dest.name}" target="_blank"><img src="{a}" alt="after"></a>'
        else:
            why = img.get("gen_failed") and f"image_gen refused: {img['gen_failed']}" or img.get("reason", "")
            after = f'<div class="skip">Skipped<br><small>{html.escape(why)}</small></div>'
        verdict = html.escape(img.get("verdict", ""))
        badge = ""
        if img.get("stage_ready") is True:
            badge = '<span class="badge ok">stage ready</span>'
        elif img.get("stage_ready") is False:
            badge = '<span class="badge no">not stage ready</span>'
        rows.append(f"""
<section class="row">
  <header><strong>{html.escape(img['file'])}</strong> <span class="role">{html.escape(img.get('role',''))}</span> {badge}</header>
  <div class="pair">
    <figure><a href="originals/{src.name}" target="_blank"><img src="{b}" alt="before"></a><figcaption>Before</figcaption></figure>
    <figure>{after}<figcaption>After</figcaption></figure>
  </div>
  {'<p class="verdict">' + verdict + '</p>' if verdict else ''}
</section>""")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Santa Skill: {html.escape(asin)}</title>
<style>
body{{margin:0;background:#faf7f2;color:#222;font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#666;margin:0 0 24px}}
.row{{background:#fff;border:1px solid #e8e2d8;border-radius:12px;padding:16px;margin-bottom:22px}}
.row header{{margin-bottom:10px}} .role{{color:#888;margin-left:8px;font-size:13px}}
.pair{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
figure{{margin:0}} figure img{{width:100%;height:auto;border-radius:8px;display:block;background:#fff}}
figcaption{{text-align:center;color:#777;font-size:13px;margin-top:6px}}
.skip{{display:flex;flex-direction:column;gap:6px;align-items:center;justify-content:center;text-align:center;padding:16px;box-sizing:border-box;aspect-ratio:1;border:2px dashed #ddd;border-radius:8px;color:#888}}
.badge{{font-size:12px;padding:2px 8px;border-radius:999px;margin-left:8px}}
.badge.ok{{background:#e3f5e6;color:#1d6b2c}} .badge.no{{background:#fde8e6;color:#a12a20}}
.verdict{{margin:10px 0 0;color:#444;font-size:14px}}
.foot{{color:#666;font-size:13px;margin-top:30px}}
@media(max-width:640px){{.pair{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">ASIN {html.escape(asin)} &middot; Christmas secondary images &middot; before / after</p>
{''.join(rows)}
<p class="foot">Generated with The Santa Skill for Codex (image_gen on your ChatGPT plan). {len(made)} image(s) made for this listing. Main image untouched per Amazon policy.</p>
</div></body></html>"""
    (run / "contact-sheet.html").write_text(page)

    lines = [f"# Santa Skill report: {asin}", "", f"Product: {title}", ""]
    t = [i for i in plan["images"] if i.get("action") == "transform"]
    s = [i for i in plan["images"] if i.get("action") != "transform"]
    lines.append(f"Transformed: {len(t)}. Skipped: {len(s)}. Total images: {len(plan['images'])}.")
    lines += ["", "## Transformed"]
    for i in t:
        if i.get("gen_failed") and not out_path(run, i["file"]).exists():
            lines.append(f"- {i['file']} ({i.get('role','')}): not generated, image_gen refused ({i['gen_failed']}).")
            continue
        ready = "stage ready" if i.get("stage_ready") else ("NOT stage ready" if i.get("stage_ready") is False else "unjudged")
        rg = " (regenerated once)" if i.get("regenerated") else ""
        lines.append(f"- {i['file']} ({i.get('role','')}): {ready}{rg}. {i.get('verdict','')}")
    lines += ["", "## Skipped"]
    for i in s:
        lines.append(f"- {i['file']} ({i.get('role','')}): {i.get('reason','')}")
    lines += ["", "## Usage",
              f"- Images made with Codex image_gen: {len(made)} "
              f"({sum(1 for u in made if u.get('regen'))} of them regenerations). They count against your ChatGPT "
              f"plan's Codex limits, not an API bill.", "",
              "Outputs: christmas/ (PNG, at the size image_gen returned). Contact sheet: contact-sheet.html."]
    (run / "report.md").write_text("\n".join(lines) + "\n")
    log(f"Wrote {run / 'contact-sheet.html'} and {run / 'report.md'}")


def cmd_sheet(args):
    write_sheet(Path(args.run))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("source"); f.add_argument("--out"); f.add_argument("--title")
    f.set_defaults(fn=cmd_fetch)
    n = sub.add_parser("next"); n.add_argument("run"); n.set_defaults(fn=cmd_next)
    l = sub.add_parser("list"); l.add_argument("run"); l.set_defaults(fn=cmd_list)
    c2 = sub.add_parser("classify"); c2.add_argument("run"); c2.add_argument("--force", action="store_true")
    c2.add_argument("--apply", nargs="?", const="", default=None, help="apply classify.json (or the file given)")
    c2.set_defaults(fn=cmd_classify)
    p = sub.add_parser("prepare"); p.add_argument("run"); p.add_argument("--only"); p.set_defaults(fn=cmd_prepare)
    pl = sub.add_parser("place"); pl.add_argument("run"); pl.add_argument("name")
    pl.add_argument("path", nargs="?", default="--latest"); pl.add_argument("--latest", action="store_true")
    pl.add_argument("--failed", help="image_gen refused or errored; record it, no retry")
    pl.set_defaults(fn=cmd_place)
    r = sub.add_parser("regen"); r.add_argument("run"); r.add_argument("name"); r.add_argument("--note")
    r.set_defaults(fn=cmd_regen)
    c = sub.add_parser("check"); c.add_argument("run"); c.set_defaults(fn=cmd_check)
    v = sub.add_parser("verify"); v.add_argument("run"); v.add_argument("--only"); v.add_argument("--force", action="store_true")
    v.add_argument("--apply", nargs="?", const="", default=None, help="apply verify.json (or the file given)")
    v.set_defaults(fn=cmd_verify)
    s = sub.add_parser("sheet"); s.add_argument("run"); s.set_defaults(fn=cmd_sheet)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
