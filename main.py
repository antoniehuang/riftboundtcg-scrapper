from __future__ import annotations

import os
import re
import sys
from pathlib import Path
import argparse
from typing import Iterable, List, Set, Dict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


URL = "https://riftbound.leagueoflegends.com/en-us/tcg-cards/"
OUT_DIR = Path("images")


def parse_srcset(srcset: str) -> List[str]:
    # Accept entries like: url 1x, url 2x OR url 320w, url 640w
    urls: List[str] = []
    for part in srcset.split(","):
        url = part.strip().split(" ")[0].strip()
        if url:
            urls.append(url)
    return urls


CDN_HOST_HINT = "cdn.rgpub.io"


def extract_image_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: List[str] = []

    # <img> elements: src, data-src, srcset, data-srcset
    for img in soup.find_all("img"):
        for attr in ["src", "data-src"]:
            val = img.get(attr)
            if val:
                urls.append(urljoin(base_url, val))
        for attr in ["srcset", "data-srcset"]:
            val = img.get(attr)
            if val:
                for u in parse_srcset(val):
                    urls.append(urljoin(base_url, u))

    # <source> elements within <picture>
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for u in parse_srcset(srcset):
                urls.append(urljoin(base_url, u))

    # Inline styles with background-image: url(...)
    style_url_re = re.compile(r'url\((?:"|\')?(.*?)(?:"|\')?\)')
    for el in soup.find_all(style=True):
        style = el["style"]
        for m in style_url_re.finditer(style):
            u = m.group(1)
            if u and not u.startswith("data:"):
                urls.append(urljoin(base_url, u))

    # Also scan raw HTML for CDN image URLs that may be referenced by lazy-load scripts
    cdn_img_pattern = re.compile(
        r"https?://[\w.-]*/public/[\w/-]*/riftbound/[\w/-]*/[\w-]*/cards/[\w-]*/(?:full|thumbnail)[^\s'\"]*\.(?:jpg|jpeg|png|webp)",
        re.IGNORECASE,
    )
    for m in cdn_img_pattern.finditer(html):
        urls.append(m.group(0))

    # Deduplicate, normalize by stripping query/fragment
    def normalize(u: str) -> str:
        parsed = urlparse(u)
        return parsed._replace(query="", fragment="").geturl()

    seen: Set[str] = set()
    result: List[str] = []
    for u in urls:
        if not u:
            continue
        # Handle protocol-relative URLs
        if u.startswith("//"):
            u = "https:" + u
        key = normalize(u)
        if key not in seen:
            seen.add(key)
            result.append(u)

    # Keep only common image extensions
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    result = [u for u in result if any(urlparse(u).path.lower().endswith(ext) for ext in exts)]
    return result


def file_ext_for_url(u: str) -> str:
    path = urlparse(u).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if path.endswith(ext):
            return ext
    return ".jpg"


PREFIX_RE = re.compile(r"/riftbound/latest/([A-Z]+)/cards/")


def detect_prefix(u: str) -> str | None:
    m = PREFIX_RE.search(urlparse(u).path)
    return m.group(1) if m else None


def download_images(urls: Iterable[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    counters: Dict[str, int] = {}

    for u in urls:
        prefix = detect_prefix(u) or "misc"
        subdir = out_dir / prefix
        subdir.mkdir(parents=True, exist_ok=True)
        next_idx = counters.get(prefix, 1)
        ext = file_ext_for_url(u)
        dest = subdir / f"{next_idx:03d}{ext}"
        try:
            r = requests.get(u, headers=headers, timeout=30)
            r.raise_for_status()
            dest.write_bytes(r.content)
            counters[prefix] = next_idx + 1
            print(f"Saved {dest}")
        except Exception as e:
            print(f"Failed to download {u}: {e}")


def try_head(url: str, headers: dict) -> bool:
    try:
        r = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def fallback_guess_by_prefixes(out_dir: Path, prefixes: list[str], start: int = 1, miss_limit: int = 3, asset: str | None = None) -> None:
    """
    Guess image URLs by iterating <PREFIX>-001.. for each prefix and checking CDN for existence.
    Downloads only the specified asset filename under each code (no fallback to other sizes).
    Stops per-prefix after `miss_limit` consecutive misses.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for prefix in prefixes:
        print(f"Scanning prefix {prefix}…")
        base = f"https://cdn.rgpub.io/public/live/map/riftbound/latest/{prefix}/cards/{{code}}/"
        miss_streak = 0
        subdir = out_dir / prefix
        subdir.mkdir(parents=True, exist_ok=True)
        idx = 1
        # Use a wide upper bound; we'll break on miss streak
        for i in range(start, 2000):
            code = f"{prefix}-{i:03d}"
            chosen = None
            fname = (asset or "full-desktop.jpg").lstrip("/")
            candidate = base.format(code=code) + fname
            if try_head(candidate, headers):
                chosen = candidate

            if not chosen:
                miss_streak += 1
                if miss_streak >= miss_limit:
                    print(f"No more images found for {prefix} after {miss_streak} misses. Moving on.")
                    break
                continue

            miss_streak = 0
            dest = subdir / f"{idx:03d}.jpg"
            try:
                r = requests.get(chosen, headers=headers, timeout=30)
                r.raise_for_status()
                dest.write_bytes(r.content)
                print(f"Saved {dest} from {chosen}")
                idx += 1
            except Exception as e:
                print(f"Failed to download {chosen}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Riftbound TCG image scraper (requests-only)")
    parser.add_argument("--prefixes", nargs="*", default=["OGS", "OGN"], help="Card set prefixes to scan (default: OGS OGN)")
    parser.add_argument("--start", type=int, default=1, help="Starting number for codes (default: 1)")
    parser.add_argument("--miss-limit", type=int, default=3, help="Consecutive misses before stopping a prefix (default: 3)")
    parser.add_argument(
        "--asset",
        default="full-desktop.jpg",
        help=(
            "Asset filename to fetch under each code (e.g., 'full-desktop.jpg'). "
            "No fallback is attempted if the asset is missing. Default: full-desktop.jpg"
        ),
    )
    args = parser.parse_args()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    print(f"Fetching {URL}…")
    resp = requests.get(URL, headers=headers, timeout=30)
    resp.raise_for_status()
    urls = extract_image_urls(resp.text, URL)
    if not urls:
        print("No images in static HTML. Falling back to pattern-based download (PREFIX-###)…")
        fallback_guess_by_prefixes(
            OUT_DIR,
            prefixes=args.prefixes,
            start=args.start,
            miss_limit=args.miss_limit,
            asset=args.asset,
        )
        print("Done.")
        return
    # Stable ordering so numbering is deterministic
    urls_sorted = sorted(urls)
    print(f"Found {len(urls_sorted)} image URLs. Downloading…")
    download_images(urls_sorted, OUT_DIR)
    print("Done.")


if __name__ == "__main__":
    main()
