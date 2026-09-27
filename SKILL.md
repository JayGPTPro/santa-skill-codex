---
name: santa-skill
description: >-
  The Santa Skill for Codex. Turns Amazon listings' secondary (lifestyle) images into Christmas
  versions before Q4, product untouched, main image left alone, text kept, people the same. Runs
  on the user's ChatGPT plan through Codex's built-in image_gen tool, no OpenAI API key. Input:
  one product link or ASIN, a folder of product images, or many products at once (pasted links or
  ASINs from any Amazon marketplace, a .txt of them, or a Seller Central Active Listings Report).
  Output: a christmas/ folder per product, a before/after sheet each, one page showing them all.
  Use when the user says "santa skill", "christmas images", "christmas mode", "Q4 images",
  "holiday listing images", "christmas my store", "my whole catalog", or pastes Amazon product
  links or ASINs together with christmas or holiday. Do not use for main images, for non-Amazon
  seasonal design, or for building images from scratch.
---

# The Santa Skill (Codex)

One line in: `$santa-skill B0GYY18GYC`. Out: Christmas versions of the listing's secondary images
in `santa/<ASIN>/christmas/`, a before/after `contact-sheet.html`, and a short `report.md`. The
main image is never touched (Amazon wants it on pure white). The product is never touched either:
same shape, color, angle, label. The world around it gets a Christmas you can see at thumbnail
size: a lit tree, garlands, gifts; ordinary summer scenes turn to winter; pool, beach and swimwear scenes stay sunny with Christmas decor.

Many products in: the product links pasted after `$santa-skill`. Out: the same thing for every
product, one folder per ASIN, and one `index.html` that shows the whole set dressed.

This is an AUTOPILOT skill. After the environment check passes, the run asks nothing. The only
stops are a missing input (Amazon blocked the fetch and no folder was given), the plan's usage
limit, and a network approval Codex itself asks for. Every judgment call is logged in plan.json
and shown on the sheet.

## Engine: your own tools, on the ChatGPT plan

- Images: your built-in `image_gen` tool, as an EDIT of the original photo. Never the imagegen
  skill's CLI fallback (`scripts/image_gen.py`), never an `OPENAI_API_KEY`, never another image
  model, not as a fallback. If `image_gen` is unavailable, stop and say so.
- Looking (classify and verify): you, with `view_image`.
- Everything else is the scripts: fetching, folders, the prompts, the measured checks (size and
  position of the product, brightness), the sheets. The scripts never call a model.

`image_gen` must receive the original as its edit target. Open the input with `view_image`, then
call `image_gen` with `referenced_image_paths` containing that exact absolute INPUT path.
Do not rely on the last image in the conversation, which may be a gallery or comparison sheet. It saves to `~/.codex/generated_images/`;
`santa.py place` takes the newest one from there into the listing folder.

## Files

```
SKILL.md                              this file
scripts/check-env.sh                  prints OK / MISSING with exact fix lines
scripts/santa.py                      one listing: fetch, next, classify, prepare, place, regen, check, verify, sheet
scripts/catalog.py                    many products: discover, prepare, next, status, index
references/christmas-prompt-rules.md  the rules every prompt is built from, and the self-check
```

**"continue santa"** (a new session, or after the usage limit): find the most recently updated
`santa-catalog/*/catalog.json` or `santa/*/plan.json` under the working directory and run `next`
on that folder. Everything done so far is on disk.

`S` below is this skill's `scripts/` folder (installed: `~/.agents/skills/santa-skill/scripts`).
Run everything from the user's working directory; a listing lands in `<cwd>/santa/<ASIN>/`, a
store in `<cwd>/santa-catalog/<name>/`.

## Step 0: environment (first run on a machine, or after an error)

```
bash $S/check-env.sh
```

If anything prints MISSING, show the user the fix lines and stop. Needs python3, Pillow, curl,
and (optional) opencv for the framing check. No API key.

Reading Amazon needs the internet, and Codex's sandbox has the network off by default. When a
`fetch`, `discover` or `prepare` command prints NO INTERNET FROM HERE (exit 3), run the same
command again asking for network access; the user approves it once. Nothing was lost.

## Which mode

| The user gives | Mode |
|---|---|
| one ASIN, one product link, one folder of images | single listing |
| two or more product links or ASINs (pasted, or in a .txt), or a Seller Central report | catalog |
| "my store", "my whole catalog", a store link, with no product list | ask for the list, in these words: "Paste the product links or ASINs you want dressed, your gift-worthy best sellers first. 20 is a good start. For the whole catalog, download the Active Listings Report from Seller Central (Reports > Inventory Reports) and give me the file." Then catalog mode |

Why the list and not a store link: the Brand Store is JavaScript and often shows only part of the
catalog, the seller page is bot-checked quickly, a brand search mixes in other sellers. Product
pages read cleanly.

---

# Single listing

```
python3 $S/santa.py fetch B0GYY18GYC                        # or a product link
python3 $S/santa.py fetch /path/to/images --out santa/MYPRODUCT --title "Product name"
```

One attempt, no retries. Exit 2 means Amazon blocked it or the gallery had under 2 images: relay
the printed folder fallback line word for word and STOP. Do not retry, do not open Amazon in a
browser. When the user comes back with a folder, run the folder form.

Then the loop. This is the whole run:

```
python3 $S/santa.py next santa/B0GYY18GYC
```

`next` prints ONE task. Do it exactly, then run `next` again. Repeat until it prints `DONE`.
The tasks:

- **TASK classify**: open the gallery strip, then each listed image, with `view_image`; decide
  each by the printed rules; write `classify.json`; run the printed `--apply` line. The script
  enforces the hard rules after you (01 is the main image; anything on white is skipped).
  Be the second opinion on your own calls: a lifestyle frame called infographic, or a collage
  called lifestyle, is fixed in classify.json before you apply.
- **TASK generate**: for each job, in order and one at a time: `view_image` the INPUT, `image_gen`
  an edit with `referenced_image_paths=[INPUT]` and the PROMPT exactly as printed (do not shorten, restructure or add to it; it
  is built from the rules file), then run the printed `place` line. One call per image, no retry.
  If `image_gen` refuses the image (content policy, for example a Disney-licensed product), run
  `place ... --failed "<why>"` and go on. If it fails because the plan's usage limit is reached,
  do NOT mark it failed: stop, tell the user the limit was reached and that saying "continue santa"
  later picks up here.
- **TASK verify**: open A (original) then B (Christmas) of each pair with `view_image`, answer the
  ten questions, write `verify.json`, run the `--apply` line. The script supplies an OpenCV
  scene-centre template-match estimate of scale and shift; it is advisory, not reliable product
  segmentation. Compare actual product boundaries and explicitly answer `same_framing`.
  A missing or low-confidence measurement never proves preservation. Read the small text on
  the product's label at full size: a changed digit fails the pair.
- A pair that fails gets ONE regeneration: `next` writes the failure into the scene and prints a
  generate task for it. After that it is reported as not stage ready, and the run moves on.

At DONE the script has written `contact-sheet.html` and `report.md`. Open the sheet for the user
(`open santa/<ASIN>/contact-sheet.html`).

**What you tell the user (single listing)**: four lines, then the paths. Which images were
transformed and which skipped (with the one-word reason), the per-image verdicts with anything
that drifted, how many images the run made on their plan, and the two paths:
`santa/<ASIN>/christmas/` and `santa/<ASIN>/contact-sheet.html`. Only images with `stage_ready: true` are candidates for upload; clearly identify failed checks.

---

# Catalog mode: many products

```
python3 $S/catalog.py discover "<everything the user pasted>"
python3 $S/catalog.py discover my-products.txt
python3 $S/catalog.py discover "Active+Listings+Report.txt" --host www.amazon.co.uk
python3 $S/catalog.py prepare santa-catalog/my-store        # reads every listing (needs internet)
python3 $S/catalog.py next santa-catalog/my-store           # the loop, until DONE
```

`discover` turns what was pasted into one ASIN per product on its own marketplace (share links
cost one HEAD request), drops duplicates, prints anything it skipped (relay those lines), takes
the first 50 (`--max 300` for more, `--max 0` for all) and prints how many images to expect.
Show those lines, then go straight on to `prepare` and the loop. Do not stop to ask.

`prepare` fetches every listing, one attempt each with a polite pause. Three captchas in a row
pause it; the same command an hour later retries the blocked ones once. It finds images that two
listings share (bundles, variations): those are looked at and generated once, then copied.

`next` works product by product: classify, generate, verify, the one regeneration, then that
product's contact sheet, then the next product. The tasks are the same as in single-listing mode.
Finishing products one at a time means that when the plan's limit stops the run, every finished
product is complete and uploadable. The state is on disk: in a new Codex session, "continue
santa" is just `next` on the same folder.

Plan usage: every image is one `image_gen` call, and OpenAI says image turns use the plan's limits
3 to 5 times faster than text turns. A user who wants a ceiling passes `--max-images 40` to
`discover` or `next`; products past it are held and named, and `next --max-images 80` continues.

```
python3 $S/catalog.py status santa-catalog/my-store     # one line per product
python3 $S/catalog.py index  santa-catalog/my-store     # rebuild the top page from disk
```

**What you tell the user (catalog)**: four lines, then two paths. Products done of products
found; Christmas images made and how many passed every check; images used on the plan; anything
held back (ceiling, usage limit, blocks, listings with fewer than two images). Then open
`santa-catalog/<name>/index.html` for them. Remind them that `originals/` is their way back in
January. Before handing over, open two or three contact sheets and look at the product in each
pair yourself.

---

## Quality and size

The built-in `image_gen` tool has no quality or size setting; it picks its own, and follows the
input's shape. `place` crops the result back to the original's shape when it drifts (and says
so in plan.json), and a banner wider than 3:1 is padded to 3:1 first and cropped back after,
because gpt-image-2 takes nothing wider. Amazon wants at least 1000 px on the longest side;
`place` warns when an output is smaller.

## Amazon policy notes

- Main image stays white. This skill never writes to `01`.
- Seasonal images are secondary images. Sellers swap them in for Q4 and out in January; keep
  `originals/` so they have the way back.
