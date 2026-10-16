#!/usr/bin/env python
"""Render props/labels.html and check that Gemini reads every serial exactly.

    uv run --python 3.12 --with playwright --with pillow --with google-genai \
        --with python-dotenv python props/check_labels.py

Writes into props/:
  labels.pdf          the printable A4 sheet (headless chromium)
  lbl-*.png           one crisp render per label
  lbl-*-768.png       each main label scaled to 768 px long side (a live-frame copy)
  labels-sheet-768.png  the whole sheet scaled to 768 px long side

Then asks the text model to read the S/N from each image; prints the raw
answer and PASS/FAIL per image. Exit code 0 when every read is exact.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FRAME_PX = 768  # the live pipeline sends frames with the longest side at 768 px
SERIALS = {
    "lbl-charger": "C65W-88120-8RT2",
    "lbl-controller": "0457-2213-X7T1",
    "lbl-charger-spare": "C65W-88120-8RT2",
    "lbl-controller-spare": "0457-2213-X7T1",
}
PROMPT = ("This is a photo of a shop label. Read the serial number next to S/N "
          "and reply with exactly that serial, nothing else.")
PROMPT_SHEET = ("This is a photo of a printed sheet of shop labels. List every S/N "
                "serial number you can read, one per line, nothing else.")
TOKEN = re.compile(r"[A-Z0-9-]{10,}")


def render() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 1400},
                                device_scale_factor=4)
        page.goto((HERE / "labels.html").as_uri())
        page.wait_for_timeout(300)
        page.pdf(path=str(HERE / "labels.pdf"), format="A4", print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        for label_id in SERIALS:
            page.locator(f"#{label_id}").screenshot(path=str(HERE / f"{label_id}.png"))
        page.screenshot(path=str(HERE / "labels-sheet.png"), full_page=True)
        browser.close()
    print(f"rendered props/labels.pdf and {len(SERIALS)} label PNGs")


def scale_to_768(path: Path, out: Path) -> None:
    img = Image.open(path).convert("RGB")
    longest = max(img.size)
    if longest > FRAME_PX:
        img = img.resize((round(img.width * FRAME_PX / longest),
                          round(img.height * FRAME_PX / longest)), Image.LANCZOS)
    img.convert("RGB").save(out)
    print(f"{out.name}: {img.width}x{img.height}")


def read_serial(client, model: str, path: Path, prompt: str) -> str:
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=path.read_bytes(), mime_type="image/png"), prompt],
    )
    return (resp.text or "").strip()


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.upper())


def main() -> int:
    load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY is not set")
        return 2

    render()
    for label_id in SERIALS:
        scale_to_768(HERE / f"{label_id}.png", HERE / f"{label_id}-768.png")
    scale_to_768(HERE / "labels-sheet.png", HERE / "labels-sheet-768.png")

    client = genai.Client(api_key=api_key)
    model = None
    for candidate in ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview"):
        try:
            read_serial(client, candidate, HERE / "lbl-charger.png", PROMPT)
            model = candidate
            break
        except Exception as exc:
            print(f"model {candidate} failed: {type(exc).__name__}: {str(exc)[:120]}")
    if model is None:
        print("no usable text model; giving up")
        return 2
    print(f"reading with {model}\n")

    failures = 0
    checks = [(f"{label_id}.png", label_id, SERIALS[label_id], PROMPT) for label_id in SERIALS]
    checks += [(f"{label_id}-768.png", label_id, SERIALS[label_id], PROMPT)
               for label_id in ("lbl-charger", "lbl-controller")]
    for name, label_id, expected, prompt in checks:
        answer = read_serial(client, model, HERE / name, prompt)
        found = tokens(answer)
        ok = expected in found
        failures += 0 if ok else 1
        print(f"{name}: {'PASS' if ok else 'FAIL'} expected {expected}")
        print(f"    raw: {answer!r}")

    answer = read_serial(client, model, HERE / "labels-sheet-768.png", PROMPT_SHEET)
    found = tokens(answer)
    both = all(serial in found for serial in SERIALS.values())
    failures += 0 if both else 1
    print(f"labels-sheet-768.png: {'PASS' if both else 'FAIL'} expected both serials")
    print(f"    raw: {answer!r}")

    print("\nresult:", "ALL READS EXACT" if failures == 0 else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
