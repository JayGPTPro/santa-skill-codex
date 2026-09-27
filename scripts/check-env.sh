#!/usr/bin/env bash
# The Santa Skill (Codex) environment check. Prints OK or MISSING with the exact fix line.
# Exit 0 = ready. Anything else = fix the MISSING lines first.
# No OpenAI API key is needed: the images come from Codex's built-in image_gen tool.

PASS=0; FAIL=0
ok()   { printf "  OK       %s\n" "$1"; PASS=$((PASS+1)); }
miss() { printf "  MISSING  %s\n           fix: %s\n" "$1" "$2"; FAIL=$((FAIL+1)); }

echo "santa-skill (Codex): environment check"
echo

# 1. python3
if command -v python3 >/dev/null 2>&1; then
  ok "python3 $(python3 -V 2>&1 | awk '{print $2}')"
else
  miss "python3" "macOS: brew install python3 | Windows: winget install Python.Python.3.12 | Linux: apt install python3"
fi

# 2. Pillow (image sizes, crops, the before/after sheets)
if python3 -c "import PIL" >/dev/null 2>&1; then
  ok "Pillow"
else
  miss "Pillow python package" "python3 -m pip install pillow   (if pip refuses: python3 -m pip install pillow --break-system-packages)"
fi

# 3. OpenCV (optional: measures whether the product kept its size in the frame)
if python3 -c "import cv2, numpy" >/dev/null 2>&1; then
  ok "opencv (framing check)"
else
  printf "  OPTIONAL %s\n           fix: %s\n" "opencv: without it the framing check is skipped" "python3 -m pip install opencv-python-headless"
fi

# 4. curl (Amazon fetch)
if command -v curl >/dev/null 2>&1; then
  ok "curl"
else
  miss "curl" "macOS ships it. Linux: apt install curl. Windows 10+: ships it, or winget install cURL.cURL"
fi

# 5. Codex image generation (informational: the skill runs inside Codex, which may not be on PATH here)
if command -v codex >/dev/null 2>&1; then
  if codex features list 2>/dev/null | grep -E '^image_generation[[:space:]]' | grep -q 'true'; then
    ok "codex $(codex --version 2>/dev/null | awk '{print $NF}'), image generation on"
  else
    miss "Codex image generation" "codex features enable image_generation   (then start a new Codex session)"
  fi
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "Ready. $PASS checks passed."
  exit 0
else
  echo "$FAIL missing. Run the fix lines above, then run this check again."
  exit 1
fi
