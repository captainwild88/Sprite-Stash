#!/usr/bin/env python3
"""Fetch fortnite.gg/sprites, update the local Sprite Stash data, and print any new sprites.

Designed to run unattended (daily routine). Exit 0 on success, 1 on a fetch or write failure.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

SOURCE_URL = "https://fortnite.gg/sprites"
BASE_URL = "https://fortnite.gg"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 45
MAX_IMAGE_WORKERS = 12

ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "sprites.json"
IMAGES_DIR = ROOT / "images"
DATA_JS_PATH = ROOT / "sprites-data.js"

# Fallback labels if the page markup changes
DEFAULT_SEASONS = {"41": "C7 S3", "42": "C7 S4", "43": "C7 MS"}
DEFAULT_VARIANTS = {
    "base": "Base",
    "cheatmaster": "Cheat Master",
    "hacker": "Loot Hacker",
    "reaper": "Bounty Hunter",
    "tricktreat": "Trick or Treat",
    "gold": "Gold",
    "candy": "Gummy",
    "galaxy": "Galaxy",
    "gem": "Gem",
    "holofoil": "Holofoil",
    "cube": "Cube",
    "quack": "Quack",
    "cursed": "Cursed",
    "jeweled": "Fortune",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def abs_url(src: str) -> str:
    src = (src or "").strip()
    if not src:
        return ""
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if not src.startswith("/"):
        src = "/" + src
    return BASE_URL + src


def parse_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    seasons = dict(DEFAULT_SEASONS)
    season_filters = soup.select("#sprites-season-filters [data-sprite-season]")
    for el in season_filters:
        key = (el.get("data-sprite-season") or "").strip()
        label = el.get_text(" ", strip=True)
        if key and key != "all" and label:
            seasons[key] = label

    variants = dict(DEFAULT_VARIANTS)
    for el in soup.select("#sprites-variant-filter .filter-select-btn[data-val]"):
        key = (el.get("data-val") or "").strip()
        label = el.get_text(" ", strip=True)
        if key and label:
            variants[key] = label

    sprites: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for card in soup.select(".sprite-card[data-sprite]"):
        sid = (card.get("data-sprite") or "").strip()
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)

        img = card.select_one("img")
        raw_src = ""
        alt = ""
        if img:
            raw_src = (img.get("src") or img.get("data-src") or "").strip()
            alt = unescape(img.get("alt") or "").strip()

        name_el = card.select_one(".sprite-name")
        name = unescape(name_el.get_text(" ", strip=True)) if name_el else ""
        if not name:
            name = alt or f"Sprite {sid}"

        href = ""
        if name_el and name_el.get("href"):
            href = name_el.get("href") or ""
        else:
            art = card.select_one("a.sprite-art")
            href = (art.get("href") if art else "") or ""

        parent = (card.get("data-parent") or "").strip()
        parent_name = unescape(card.get("data-parent-name") or "").strip() or parent
        rarity = (card.get("data-rarity") or "").strip()
        variant = (card.get("data-variant") or "").strip() or "base"
        season = (card.get("data-season") or "").strip()
        is_new = (card.get("data-is-new") or "").strip() == "1"
        unreleased = (card.get("data-unreleased") or "").strip() == "1"
        image_url = abs_url(raw_src)

        filename = ""
        if image_url:
            stem = Path(image_url.split("?")[0]).name
            if not stem:
                stem = f"{sid}.webp"
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
            filename = f"{sid}-{safe}"

        sprites.append(
            {
                "id": sid,
                "name": name,
                "parent": parent,
                "parent_name": parent_name,
                "rarity": rarity,
                "variant": variant,
                "variant_name": variants.get(variant, variant.replace("_", " ").title()),
                "season": season,
                "season_name": seasons.get(season, f"Season {season}" if season else ""),
                "is_new": is_new,
                "unreleased": unreleased,
                "image_url": image_url,
                "image": f"images/{filename}" if filename else "",
                "page_url": abs_url(href) if href else "",
            }
        )

    # Keep a stable order: current season first, then id
    def sort_key(s: dict[str, Any]) -> tuple:
        try:
            season_n = -int(s.get("season") or 0)
        except ValueError:
            season_n = 0
        try:
            id_n = int(s.get("id") or 0)
        except ValueError:
            id_n = 0
        return (season_n, id_n, s.get("name") or "")

    sprites.sort(key=sort_key)
    return {"seasons": seasons, "variants": variants, "sprites": sprites}


def load_existing() -> dict[str, Any]:
    if not JSON_PATH.exists():
        return {"sprites": []}
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {JSON_PATH}: {exc}", file=sys.stderr)
        return {"sprites": []}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        tmp_name = tmp.name
    Path(tmp_name).replace(path)


def write_json(payload: dict[str, Any]) -> None:
    atomic_write(JSON_PATH, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def write_data_js(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False)
    atomic_write(DATA_JS_PATH, f"window.SPRITE_STASH_DATA = {body};\n")


def download_one(image_url: str, dest: Path) -> tuple[str, bool, str]:
    if dest.exists() and dest.stat().st_size > 0:
        return (image_url, True, "exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = http_get(image_url)
        if not data:
            return (image_url, False, "empty response")
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return (image_url, True, "downloaded")
    except Exception as exc:  # noqa: BLE001 — report and continue
        return (image_url, False, str(exc))


def download_images(sprites: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    jobs: list[tuple[str, Path]] = []
    for s in sprites:
        image_url = s.get("image_url") or ""
        rel = s.get("image") or ""
        if not image_url or not rel:
            continue
        jobs.append((image_url, ROOT / rel))

    ok = 0
    failed: list[str] = []
    if not jobs:
        return 0, 0, failed

    with ThreadPoolExecutor(max_workers=MAX_IMAGE_WORKERS) as pool:
        futures = {pool.submit(download_one, url, dest): (url, dest) for url, dest in jobs}
        for fut in as_completed(futures):
            url, dest = futures[fut]
            try:
                _, success, msg = fut.result()
            except Exception as exc:  # noqa: BLE001
                success, msg = False, str(exc)
            if success:
                ok += 1
            else:
                failed.append(f"{url} -> {dest.name}: {msg}")
                print(f"Image download failed: {url} ({msg})", file=sys.stderr)
    return ok, len(jobs), failed


def main() -> int:
    print(f"Fetching {SOURCE_URL}")
    try:
        raw = http_get(SOURCE_URL)
        # fortnite.gg serves gzip; urllib handles it. Decode as utf-8.
        html = raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"ERROR: failed to fetch sprites page: {exc}", file=sys.stderr)
        return 1

    parsed = parse_page(html)
    sprites = parsed["sprites"]
    if not sprites:
        print("ERROR: page fetched but no sprites were found. Markup may have changed.", file=sys.stderr)
        return 1

    existing = load_existing()
    old_by_id = {
        str(s.get("id")): s
        for s in (existing.get("sprites") or [])
        if s.get("id") is not None
    }
    new_ones = [s for s in sprites if s["id"] not in old_by_id]
    gone = [s for s in old_by_id.values() if str(s.get("id")) not in {x["id"] for x in sprites}]

    ok, total, failed = download_images(sprites)
    # If a download failed, keep the remote URL and clear the local path so the UI can fall back
    failed_urls = {line.split(" ->", 1)[0] for line in failed}
    for s in sprites:
        if s.get("image_url") in failed_urls:
            # leave image path anyway if a previous copy exists
            rel = s.get("image") or ""
            if rel and not (ROOT / rel).exists():
                s["image"] = ""

    payload = {
        "source": SOURCE_URL,
        "updated_at": now_iso(),
        "count": len(sprites),
        "seasons": parsed["seasons"],
        "variants": parsed["variants"],
        "sprites": sprites,
    }
    try:
        write_json(payload)
        write_data_js(payload)
    except OSError as exc:
        print(f"ERROR: failed to write data files: {exc}", file=sys.stderr)
        return 1

    print(f"Sprites on page: {len(sprites)}")
    print(f"Images: {ok}/{total} available locally" + (f", {len(failed)} failed" if failed else ""))
    print(f"Updated {JSON_PATH}")

    if not old_by_id:
        print("Initial collection saved. No previous list to compare.")
    elif new_ones:
        print(f"\nFound {len(new_ones)} new sprite(s):")
        for s in new_ones:
            print(f"- {s['name']}")
            print(f"  {s['image_url']}")
    else:
        print("No new sprites.")

    if gone:
        print(f"\n{len(gone)} sprite(s) no longer listed:")
        for s in gone:
            print(f"- {s.get('name') or s.get('id')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
