#!/usr/bin/env python3
"""
=============================================================================
  WNNC Plant Image Downloader
=============================================================================
  Downloads one photo per plant from the internet using multiple fallback
  sources (Wikimedia Commons, iNaturalist, GBIF).

  Usage:
      python download_plant_images.py [OPTIONS]

  Options:
      --input   PATH   Path to CSV file with plant names  (default: Plant_impges_list.csv)
      --outdir  PATH   Folder to save images              (default: C:\gcc\AI\WNNCA_Plants\images)
      --log     PATH   Path to write the download log     (default: <outdir>\download_log.csv)
      --delay   SECS   Seconds to pause between requests  (default: 1.5)
      --col     NAME   Column name in CSV that has plant names (default: "Plant Name")

  Examples:
      python download_plant_images.py
      python download_plant_images.py --input my_plants.csv --outdir D:\Photos\Plants
      python download_plant_images.py --input plants.csv --outdir /home/user/plants --delay 2

  Requirements:
      pip install requests pillow
=============================================================================
"""

import argparse
import csv
import os
import re
import sys
import time
import logging
from pathlib import Path
from urllib.parse import quote

# ── Try to import requests & Pillow ─────────────────────────────────────────
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("ERROR: 'requests' is not installed.  Run:  pip install requests")
    sys.exit(1)

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False   # not fatal — we just skip image validation

# ── Constants ────────────────────────────────────────────────────────────────
DEFAULT_INPUT   = "Plant_impges_list.csv"
DEFAULT_OUTDIR  = r"C:\gcc\AI\WNNCA_Plants\images"
DEFAULT_COL     = "Plant Name"
DEFAULT_DELAY   = 1.5       # seconds between requests
TIMEOUT         = 20        # HTTP timeout in seconds
MIN_BYTES       = 5_000     # images smaller than this are likely placeholders
USER_AGENT      = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 WNNC-PlantBot/1.0"
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("plant_dl")


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def plant_to_filename(plant_name: str) -> str:
    """
    Convert a plant name to a safe filename.
      "Cholla (Teddy Bear)"      → Cholla_Teddy_Bear.jpg
      "Narrow-leaf Plantain"     → Narrow_leaf_Plantain.jpg
      "Farewell-to-Spring"       → Farewell_to_Spring.jpg
      "Pigweed (Redroot)"        → Pigweed_Redroot.jpg
      "Bird's-foot Fern"         → Bird_s_foot_Fern.jpg
      "Ithuriel's Spear"         → Ithuriel_s_Spear.jpg
    """
    name = plant_name.strip()
    # Replace special characters and spaces with underscore
    name = re.sub(r"[^A-Za-z0-9]+", "_", name)
    # Strip leading/trailing underscores
    name = name.strip("_")
    return name + ".jpg"


def build_session() -> requests.Session:
    """Return a requests Session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


# ═══════════════════════════════════════════════════════════════════════════
#  IMAGE SOURCE STRATEGIES
#  Each function tries to find a direct image URL for the plant.
#  Returns (image_url, source_label) or (None, None) on failure.
# ═══════════════════════════════════════════════════════════════════════════

def try_wikimedia(session: requests.Session, plant_name: str):
    """
    Source 1 — Wikimedia Commons via the MediaWiki API.
    Searches for the plant name, picks the first result, then gets its
    image URL via the imageinfo API.
    """
    try:
        # Step A: search for a page about the plant
        search_url = (
            "https://en.wikipedia.org/w/api.php"
            "?action=query&list=search"
            f"&srsearch={quote(plant_name + ' plant')}"
            "&srlimit=3&format=json"
        )
        r = session.get(search_url, timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("query", {}).get("search", [])
        if not results:
            return None, None

        page_title = results[0]["title"]

        # Step B: get the main image for that Wikipedia page
        img_url = (
            "https://en.wikipedia.org/w/api.php"
            "?action=query&titles=" + quote(page_title) +
            "&prop=pageimages&pithumbsize=800&format=json"
        )
        r2 = session.get(img_url, timeout=TIMEOUT)
        r2.raise_for_status()
        pages = r2.json().get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = page.get("thumbnail", {}).get("source")
            if thumb:
                return thumb, f"Wikipedia/{page_title}"

        return None, None

    except Exception as exc:
        log.debug("Wikimedia error for '%s': %s", plant_name, exc)
        return None, None


def try_inaturalist(session: requests.Session, plant_name: str):
    """
    Source 2 — iNaturalist API.
    Searches taxa, then retrieves a photo from the first matching taxon.
    """
    try:
        taxa_url = (
            "https://api.inaturalist.org/v1/taxa"
            f"?q={quote(plant_name)}&rank=species,subspecies,variety"
            "&per_page=3&locale=en"
        )
        r = session.get(taxa_url, timeout=TIMEOUT)
        r.raise_for_status()
        taxa = r.json().get("results", [])
        if not taxa:
            return None, None

        # Find the first taxon that has a default photo
        for taxon in taxa:
            photo = taxon.get("default_photo")
            if photo:
                url = photo.get("medium_url") or photo.get("url")
                if url:
                    # iNaturalist medium_url uses /medium/ — upgrade to /large/
                    url = url.replace("/medium.", "/large.").replace("/square.", "/large.")
                    return url, f"iNaturalist/taxon-{taxon.get('id')}"

        return None, None

    except Exception as exc:
        log.debug("iNaturalist error for '%s': %s", plant_name, exc)
        return None, None


def try_gbif(session: requests.Session, plant_name: str):
    """
    Source 3 — GBIF (Global Biodiversity Information Facility) image API.
    """
    try:
        # Step A: species lookup
        lookup_url = (
            "https://api.gbif.org/v1/species/suggest"
            f"?q={quote(plant_name)}&limit=3"
        )
        r = session.get(lookup_url, timeout=TIMEOUT)
        r.raise_for_status()
        suggestions = r.json()
        if not suggestions:
            return None, None

        # Take the first result with a usageKey
        for s in suggestions:
            usage_key = s.get("key")
            if not usage_key:
                continue

            # Step B: media endpoint
            media_url = (
                f"https://api.gbif.org/v1/occurrence/search"
                f"?taxonKey={usage_key}&mediaType=StillImage&limit=5"
            )
            r2 = session.get(media_url, timeout=TIMEOUT)
            r2.raise_for_status()
            occurrences = r2.json().get("results", [])
            for occ in occurrences:
                for media in occ.get("media", []):
                    img = media.get("identifier")
                    if img and img.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                        return img, f"GBIF/taxonKey-{usage_key}"

        return None, None

    except Exception as exc:
        log.debug("GBIF error for '%s': %s", plant_name, exc)
        return None, None


# ═══════════════════════════════════════════════════════════════════════════
#  DOWNLOAD LOGIC
# ═══════════════════════════════════════════════════════════════════════════

STRATEGIES = [
    ("Wikimedia/Wikipedia", try_wikimedia),
    ("iNaturalist",         try_inaturalist),
    ("GBIF",                try_gbif),
]


def download_image(session: requests.Session, img_url: str, dest_path: Path) -> bool:
    """
    Download an image from img_url and save it to dest_path.
    Returns True on success, False on failure.
    """
    try:
        r = session.get(img_url, timeout=TIMEOUT, stream=True)
        r.raise_for_status()

        # Check content type
        ct = r.headers.get("content-type", "")
        if "image" not in ct and "octet-stream" not in ct:
            log.debug("Non-image content-type '%s' from %s", ct, img_url)
            return False

        data = r.content
        if len(data) < MIN_BYTES:
            log.debug("Image too small (%d bytes) — likely a placeholder", len(data))
            return False

        # Validate with Pillow if available
        if PIL_AVAILABLE:
            try:
                from io import BytesIO
                img = Image.open(BytesIO(data))
                img.verify()           # raises if corrupt
            except Exception:
                log.debug("Pillow rejected image from %s", img_url)
                return False

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True

    except Exception as exc:
        log.debug("Download failed for %s: %s", img_url, exc)
        return False


def fetch_plant_image(session: requests.Session,
                      plant_name: str,
                      dest_path: Path,
                      delay: float) -> tuple:
    """
    Try each strategy in order.  On the first successful download, return
    (True, image_filename, source_url).  Otherwise return (False, filename, "").
    """
    filename = dest_path.name

    for strategy_name, strategy_fn in STRATEGIES:
        log.debug("  Trying %s …", strategy_name)
        img_url, source_label = strategy_fn(session, plant_name)
        time.sleep(delay * 0.5)          # polite pause between API calls

        if not img_url:
            log.debug("  %s: no URL found", strategy_name)
            continue

        log.debug("  %s: found %s", strategy_name, img_url)
        success = download_image(session, img_url, dest_path)
        time.sleep(delay * 0.5)

        if success:
            log.info("  ✓  Saved via %s → %s", strategy_name, filename)
            return True, filename, img_url

        log.debug("  %s: download failed", strategy_name)

    log.warning("  ✗  All sources failed for '%s'", plant_name)
    return False, filename, ""


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Download plant images and create a log CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", "-i",
        default=DEFAULT_INPUT,
        help=f"Input CSV file with plant names  (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--outdir", "-o",
        default=DEFAULT_OUTDIR,
        help=f"Folder to save images  (default: {DEFAULT_OUTDIR})",
    )
    parser.add_argument(
        "--log", "-l",
        default=None,
        help="Path to write download log CSV  (default: <outdir>/download_log.csv)",
    )
    parser.add_argument(
        "--delay", "-d",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Seconds to pause between requests  (default: {DEFAULT_DELAY})",
    )
    parser.add_argument(
        "--col", "-c",
        default=DEFAULT_COL,
        help=f'Column name in CSV for plant names  (default: "{DEFAULT_COL}")',
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip plants whose image file already exists  (default: True)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Re-download even if the image file already exists",
    )
    return parser.parse_args()


def read_plant_names(csv_path: str, col_name: str) -> list:
    """Read plant names from a CSV file, stripping blanks and duplicates."""
    path = Path(csv_path)
    if not path.exists():
        log.error("Input file not found: %s", csv_path)
        sys.exit(1)

    names = []
    seen  = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        # Auto-detect whether there is a header
        sample = f.read(2048)
        f.seek(0)
        has_header = csv.Sniffer().has_header(sample)
        reader = csv.DictReader(f) if has_header else csv.reader(f)

        for row in reader:
            if isinstance(row, dict):
                # DictReader
                # Try the requested column, then the first column
                name = row.get(col_name) or next(iter(row.values()), "")
            else:
                # plain reader — take first column
                name = row[0] if row else ""

            name = name.strip()
            if name and name.lower() != col_name.lower() and name not in seen:
                names.append(name)
                seen.add(name)

    return names


def main():
    args = parse_args()

    outdir   = Path(args.outdir)
    log_path = Path(args.log) if args.log else outdir / "download_log.csv"
    skip_existing = not args.overwrite

    outdir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("=" * 62)
    log.info("  WNNC Plant Image Downloader")
    log.info("=" * 62)
    log.info("  Input  : %s", args.input)
    log.info("  Output : %s", outdir)
    log.info("  Log    : %s", log_path)
    log.info("  Delay  : %.1f s", args.delay)
    log.info("=" * 62)

    plant_names = read_plant_names(args.input, args.col)
    total = len(plant_names)
    log.info("  Found %d plant names to process.", total)

    session = build_session()

    # ── Load existing log (so we can append / merge) ─────────────────────
    existing_log = {}
    if log_path.exists():
        with open(log_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing_log[row.get("Plant Name", "")] = row

    results = []   # list of dicts for final log
    ok_count   = 0
    fail_count = 0
    skip_count = 0

    for i, plant_name in enumerate(plant_names, 1):
        filename = plant_to_filename(plant_name)
        dest     = outdir / filename

        log.info("[%d/%d] %s  →  %s", i, total, plant_name, filename)

        # Skip if already downloaded
        if skip_existing and dest.exists() and dest.stat().st_size >= MIN_BYTES:
            log.info("  ⏭  Already exists — skipping.")
            src_url = existing_log.get(plant_name, {}).get("Source URL", "(existing)")
            results.append({
                "Plant Name":        plant_name,
                "Image File Name":   filename,
                "Source URL":        src_url,
                "Status":            "Skipped (existing)",
            })
            skip_count += 1
            continue

        success, fname, src_url = fetch_plant_image(session, plant_name, dest, args.delay)

        if success:
            ok_count += 1
            status = "Downloaded"
        else:
            fail_count += 1
            status = "FAILED"

        results.append({
            "Plant Name":        plant_name,
            "Image File Name":   fname,
            "Source URL":        src_url,
            "Status":            status,
        })

        # Polite delay between plants
        if i < total:
            time.sleep(args.delay)

    # ── Write log CSV ─────────────────────────────────────────────────────
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["Plant Name", "Image File Name", "Source URL", "Status"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    # ── Summary ───────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 62)
    log.info("  DONE")
    log.info("  ✓  Downloaded : %d", ok_count)
    log.info("  ⏭  Skipped   : %d", skip_count)
    log.info("  ✗  Failed    : %d", fail_count)
    log.info("  Log saved to : %s", log_path)
    log.info("=" * 62)

    # Print failed plants so the user knows what to fix manually
    failed = [r for r in results if r["Status"] == "FAILED"]
    if failed:
        log.warning("")
        log.warning("  Plants with NO image downloaded (%d):", len(failed))
        for r in failed:
            log.warning("    • %s", r["Plant Name"])

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
