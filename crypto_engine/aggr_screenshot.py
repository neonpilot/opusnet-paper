#!/usr/bin/env python
"""Headless-Chrome screenshots of the aggressive page; exits 1 on any JS error.
  python -m crypto_engine.aggr_screenshot [url-or-path] [outdir]"""
import pathlib, sys
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
target = sys.argv[1] if len(sys.argv) > 1 else (ROOT / "crypto-aggressive" / "index.html").as_uri()
out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path("/workspace/crypto_shots/aggressive")
out.mkdir(parents=True, exist_ok=True)
shots = [("aggr_live_desktop.png", {"width": 1440, "height": 1600}, "", 1),
         ("aggr_live_phone.png", {"width": 390, "height": 844}, "", 2),
         ("aggr_replay_180_desktop.png", {"width": 1440, "height": 1600}, "#replay=180&pause", 1),
         ("aggr_replay_end_desktop.png", {"width": 1440, "height": 1600}, "#replay=999&pause", 1),
         ("aggr_replay_end_phone.png", {"width": 390, "height": 844}, "#replay=999&pause", 2),
         ("aggr_caveats_desktop.png", {"width": 1440, "height": 1600}, "#caveats", 1)]
errors, notes = [], []
with sync_playwright() as p:
    kw = {"executable_path": "/usr/bin/google-chrome"} if pathlib.Path("/usr/bin/google-chrome").exists() else {}
    b = p.chromium.launch(headless=True, args=["--no-sandbox"], **kw)
    for name, vp, hsh, dpr in shots:
        pg = b.new_page(viewport=vp, device_scale_factor=dpr, is_mobile=vp["width"] < 500, has_touch=vp["width"] < 500)
        pg.on("pageerror", lambda e, name=name: errors.append(f"{name}: {e}"))
        def on_console(m, name=name):
            if m.type == "error":
                (notes if "Failed to load resource" in m.text else errors).append(f"{name}: {m.text}")
        pg.on("console", on_console)
        pg.goto(target + hsh)
        pg.wait_for_timeout(3500)
        if not pg.evaluate("window.__opusnet_ok === true"):
            errors.append(f"{name}: app did not finish booting")
        pg.screenshot(path=str(out / name), full_page=hsh != "#caveats")
        pg.close()
    b.close()
print("screenshots:", ", ".join(str(out / s[0]) for s in shots))
print("JS errors:", errors or "none")
if notes:
    print("network notes (not JS errors):", notes)
sys.exit(1 if errors else 0)
