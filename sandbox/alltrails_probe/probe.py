#!/usr/bin/env python3
"""
AllTrails metadata probe (isolated experiment).

Extracts trail description and first image URL from AllTrails pages, with optional
Playwright fallback when static HTML is insufficient.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class TrailInput:
    name: str
    region: str = ""
    alltrails_url: str = ""


@dataclass
class ProbeResult:
    name: str
    region: str
    alltrails_url: str
    resolved_url: str
    description: str
    image_url: str
    image_path: str
    method: str
    status: str
    error: str


def load_inputs(path: Path) -> list[TrailInput]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[TrailInput] = []
    for row in data:
        out.append(
            TrailInput(
                name=str(row.get("name", "")).strip(),
                region=str(row.get("region", "")).strip(),
                alltrails_url=str(row.get("alltrails_url", "")).strip(),
            )
        )
    return [t for t in out if t.name]


def sanitize_filename(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", text.strip())
    return s[:120] or "trail"


def domain_is_alltrails(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("alltrails.com")


def looks_like_trail_page(url: str) -> bool:
    p = urlparse(url)
    return domain_is_alltrails(url) and "/trail/" in p.path


def discover_alltrails_url(name: str, region: str, timeout: int = 12) -> str:
    query = f"site:alltrails.com trail {name} {region}".strip()
    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(search_url, headers=headers, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if looks_like_trail_page(href):
            return href
    for a in soup.select("a"):
        href = a.get("href", "")
        if looks_like_trail_page(href):
            return href
    return ""


def extract_from_ld_json(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    description = ""
    image_url = ""

    for script in scripts:
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            if "@graph" in payload and isinstance(payload["@graph"], list):
                candidates.extend([x for x in payload["@graph"] if isinstance(x, dict)])
            candidates.append(payload)
        elif isinstance(payload, list):
            candidates.extend([x for x in payload if isinstance(x, dict)])

        for node in candidates:
            d = node.get("description")
            if not description and isinstance(d, str) and d.strip():
                description = d.strip()

            img = node.get("image")
            if not image_url:
                if isinstance(img, str) and img.startswith("http"):
                    image_url = img
                elif isinstance(img, list):
                    for v in img:
                        if isinstance(v, str) and v.startswith("http"):
                            image_url = v
                            break
                elif isinstance(img, dict):
                    u = img.get("url")
                    if isinstance(u, str) and u.startswith("http"):
                        image_url = u

            if description and image_url:
                return description, image_url

    return description, image_url


def extract_from_meta(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    desc = ""
    img = ""

    m_desc = soup.find("meta", attrs={"name": "description"})
    if m_desc and m_desc.get("content"):
        desc = m_desc["content"].strip()

    og_desc = soup.find("meta", attrs={"property": "og:description"})
    if not desc and og_desc and og_desc.get("content"):
        desc = og_desc["content"].strip()

    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img and og_img.get("content"):
        img = og_img["content"].strip()

    tw_img = soup.find("meta", attrs={"name": "twitter:image"})
    if not img and tw_img and tw_img.get("content"):
        img = tw_img["content"].strip()

    return desc, img


def fetch_static(url: str, timeout: int = 15) -> tuple[str, str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    html = r.text

    d1, i1 = extract_from_ld_json(html)
    d2, i2 = extract_from_meta(html)

    description = d1 or d2
    image_url = i1 or i2
    return str(r.url), description, image_url


def fetch_playwright(url: str, timeout_ms: int = 20000) -> tuple[str, str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        final_url = page.url
        html = page.content()
        browser.close()

    d1, i1 = extract_from_ld_json(html)
    d2, i2 = extract_from_meta(html)
    description = d1 or d2
    image_url = i1 or i2
    return final_url, description, image_url


def download_image(image_url: str, out_path: Path) -> str:
    if not image_url:
        return ""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(image_url, headers=headers, timeout=20)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return str(out_path)


def probe_one(
    trail: TrailInput,
    discover_url: bool,
    use_playwright: bool,
    download_images: bool,
    images_dir: Path,
    delay_seconds: float,
) -> ProbeResult:
    result = ProbeResult(
        name=trail.name,
        region=trail.region,
        alltrails_url=trail.alltrails_url,
        resolved_url="",
        description="",
        image_url="",
        image_path="",
        method="",
        status="",
        error="",
    )

    try:
        url = trail.alltrails_url.strip()
        if not url and discover_url:
            url = discover_alltrails_url(trail.name, trail.region)
        if not url:
            result.status = "no_url"
            result.error = "No AllTrails URL provided or discovered"
            return result
        if not looks_like_trail_page(url):
            result.status = "invalid_url"
            result.error = "Resolved URL is not an AllTrails trail page"
            result.resolved_url = url
            return result

        static_error = ""
        try:
            final_url, desc, img = fetch_static(url)
            result.method = "static"
            result.resolved_url = final_url
            result.description = desc
            result.image_url = img
        except Exception as e:
            static_error = str(e)

        if use_playwright and (not result.description or not result.image_url):
            final_url_pw, desc_pw, img_pw = fetch_playwright(url)
            if desc_pw or img_pw:
                result.method = "playwright"
                result.resolved_url = final_url_pw
                result.description = desc_pw or result.description
                result.image_url = img_pw or result.image_url

        if static_error and not result.description and not result.image_url:
            result.error = static_error

        if download_images and result.image_url:
            suffix = Path(urlparse(result.image_url).path).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                suffix = ".jpg"
            fname = sanitize_filename(f"{trail.name}_{trail.region}") + suffix
            result.image_path = download_image(result.image_url, images_dir / fname)

        if result.description or result.image_url:
            result.status = "ok"
        else:
            result.status = "partial_empty"
            if not result.error:
                result.error = "Fetched page but found no description/image"

        if delay_seconds > 0:
            time.sleep(delay_seconds)

        return result
    except Exception as e:
        result.status = "error"
        result.error = str(e)
        return result


def write_outputs(results: list[ProbeResult], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    csv_path = out_dir / "results.csv"

    json_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=True),
        encoding="utf-8",
    )

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "name",
                "region",
                "alltrails_url",
                "resolved_url",
                "description",
                "image_url",
                "image_path",
                "method",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe AllTrails trail metadata.")
    parser.add_argument("--input", default="sample_trails.json", help="Path to input JSON array")
    parser.add_argument("--out-dir", default="outputs", help="Output folder")
    parser.add_argument("--discover-url", action="store_true", help="Try to find AllTrails URL via DuckDuckGo")
    parser.add_argument("--use-playwright", action="store_true", help="Use Playwright fallback if static parse is empty")
    parser.add_argument("--download-images", action="store_true", help="Download first image locally")
    parser.add_argument("--delay", type=float, default=1.5, help="Delay between trails in seconds")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    input_path = (base / args.input).resolve() if not Path(args.input).is_absolute() else Path(args.input)
    out_dir = (base / args.out_dir).resolve() if not Path(args.out_dir).is_absolute() else Path(args.out_dir)
    images_dir = out_dir / "images"

    trails = load_inputs(input_path)
    results: list[ProbeResult] = []

    for idx, t in enumerate(trails, start=1):
        print(f"[{idx}/{len(trails)}] {t.name} ({t.region})")
        r = probe_one(
            trail=t,
            discover_url=args.discover_url,
            use_playwright=args.use_playwright,
            download_images=args.download_images,
            images_dir=images_dir,
            delay_seconds=args.delay,
        )
        print(f"  -> status={r.status}, method={r.method or '-'}")
        results.append(r)

    write_outputs(results, out_dir)

    ok = sum(1 for r in results if r.status == "ok")
    print(f"\nDone. ok={ok}/{len(results)}")
    print(f"Wrote: {out_dir / 'results.json'}")
    print(f"Wrote: {out_dir / 'results.csv'}")


if __name__ == "__main__":
    main()
