# The Santa Skill for Codex

Christmas versions of your Amazon listing images, made inside Codex on your ChatGPT plan.
No OpenAI API key, no separate bill: the images count against your plan's Codex usage.

Your main image is never touched. Your product is never touched. The scene around it gets
Christmas: a lit tree, garlands and gifts. Ordinary summer scenes become winter; pools, beaches
and swimwear stay sunny, with Christmas decorations and unchanged clothing.

## What you need

- A ChatGPT plan that includes Codex with image generation (a paid plan such as Plus or Pro).
- A Mac, Windows or Linux computer with Python 3.

## Install (once, about five minutes)

1. Install Codex and sign in with your ChatGPT account:

   ```
   npm install -g @openai/codex
   codex
   ```

   Pick "Sign in with ChatGPT" when it asks. Then type `/quit`.

2. Install the skill for Codex (one line):

   ```
   npx skills add JayGPTPro/santa-skill-codex -g -a codex
   ```

   No Node? Download this repo and copy it by hand: `mkdir -p ~/.agents/skills && cp -R santa-skill-codex ~/.agents/skills/santa-skill`
   (on Windows the folder is `%USERPROFILE%\.agents\skills`).

3. Install the two picture helpers the skill uses:

   ```
   python3 -m pip install pillow opencv-python-headless
   ```

4. Check that everything is ready:

   ```
   bash ~/.agents/skills/santa-skill/scripts/check-env.sh
   ```

   It prints OK for each line. If a line says MISSING, run the fix it prints.

## Use it

Make a folder for the work, open a terminal in it, and start Codex:

```
mkdir ~/christmas && cd ~/christmas
codex
```

Then type one of these:

- One product: `$santa-skill B0GYY18GYC` (or paste the product link after `$santa-skill`)
- Several products: `$santa-skill` and paste all the product links or ASINs after it
- Your whole catalog: download the Active Listings Report from Seller Central
  (Reports > Inventory Reports) and type `$santa-skill` with the file's path

Codex will ask once to use the internet so it can read your Amazon pages. Say yes.
After that it works on its own and tells you when it is done.

## What you get

- `santa/<ASIN>/christmas/` holds the Christmas images, ready to upload as secondary images.
- `santa/<ASIN>/contact-sheet.html` shows every image before and after, with a verdict under each.
- `santa/<ASIN>/originals/` keeps your current images, so you can switch back in January.
- For several products: `santa-catalog/my-store/index.html` shows the whole store dressed.
  Hover any image to see the original.

## Good to know

- Images use your plan's Codex limits, and image turns use them faster than chat. Start with
  one product, then your best gift sellers. If you hit the limit, come back later and type
  "continue santa". It picks up where it stopped.
- Some images are skipped on purpose: the main image, product-on-white shots, and busy
  infographics. The sheet says why for each one.
- If the image tool refuses an edit, the report records the actual reason and continues.
- If Amazon blocks the download, save the listing images into a folder and type
  `$santa-skill /path/to/that/folder`.
