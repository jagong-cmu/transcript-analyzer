"""Local mirror of the two libraries the PDF renderer needs in the browser.

Mermaid and KaTeX are fetched once from a CDN and cached under
`data/render-assets/<pin>/`; every later render reads them from disk. That
matters because this runs unattended: a study-notes PDF that silently lost its
diagrams because a CDN blipped would look like a rendering bug for weeks, and
the alternative — refusing to render at all when the network is unavailable —
would fail on the exact recording the captain just made on a plane.

Pinned versions, never "latest": the renderer's diagram validation is written
against these, and an upstream release must be an explicit change here.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)

KATEX_VERSION = "0.16.11"
MERMAID_VERSION = "11.4.1"
# Bump when a pin changes so a stale cache is never mixed with a new one.
ASSET_PIN = f"katex-{KATEX_VERSION}+mermaid-{MERMAID_VERSION}"

_CDN = "https://cdn.jsdelivr.net/npm"
_FILES = {
    "mermaid.min.js": f"{_CDN}/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js",
    "katex.min.js": f"{_CDN}/katex@{KATEX_VERSION}/dist/katex.min.js",
    "katex.min.css": f"{_CDN}/katex@{KATEX_VERSION}/dist/katex.min.css",
}
# A truncated download would fail in the browser with no useful message, so a
# file smaller than its floor is treated as not downloaded at all.
_MIN_BYTES = {
    "mermaid.min.js": 500_000,
    "katex.min.js": 100_000,
    "katex.min.css": 5_000,
}
_FONT_REF_RE = re.compile(r"url\(fonts/([A-Za-z0-9_\-]+\.woff2)\)")

_TIMEOUT = 60.0


class AssetError(RuntimeError):
    """The render assets are neither cached nor downloadable."""


def assets_dir(data_dir: Path) -> Path:
    return data_dir / "render-assets" / ASSET_PIN


def have_assets(data_dir: Path) -> bool:
    """Whether a complete cache is already on disk (no network needed)."""
    root = assets_dir(data_dir)
    return all(_is_complete(root / name, name) for name in _FILES)


def ensure_assets(data_dir: Path) -> Path:
    """The directory holding mermaid, KaTeX, and KaTeX's woff2 fonts.

    Downloads only what is missing, so the common case touches no network.
    """
    root = assets_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    for name, url in _FILES.items():
        dest = root / name
        if _is_complete(dest, name):
            continue
        _download(url, dest, _MIN_BYTES.get(name, 1))
    _ensure_fonts(root)
    return root


def stage_assets(data_dir: Path, dest: Path) -> None:
    """Copy the cached assets next to a page about to be rendered.

    The page loads them with RELATIVE paths, which is what lets KaTeX's CSS
    find its own fonts (`url(fonts/…)`) without rewriting the stylesheet.
    """
    root = ensure_assets(data_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for name in _FILES:
        shutil.copyfile(root / name, dest / name)
    fonts = root / "fonts"
    if fonts.is_dir():
        shutil.copytree(fonts, dest / "fonts", dirs_exist_ok=True)


def _is_complete(path: Path, name: str) -> bool:
    try:
        return path.stat().st_size >= _MIN_BYTES.get(name, 1)
    except OSError:
        return False


def _ensure_fonts(root: Path) -> None:
    """Mirror the woff2 faces KaTeX's own stylesheet asks for.

    Only woff2: the stylesheet lists woff and ttf as later fallbacks, and a
    browser that found the woff2 never requests them.
    """
    css = (root / "katex.min.css").read_text(encoding="utf-8")
    fonts = root / "fonts"
    fonts.mkdir(exist_ok=True)
    for name in sorted(set(_FONT_REF_RE.findall(css))):
        dest = fonts / name
        if dest.exists() and dest.stat().st_size > 0:
            continue
        _download(
            f"{_CDN}/katex@{KATEX_VERSION}/dist/fonts/{name}", dest, 1
        )


def _download(url: str, dest: Path, min_bytes: int) -> None:
    import httpx

    try:
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 - every failure means the same thing here
        raise AssetError(f"could not fetch {url}: {e}") from e
    if len(resp.content) < min_bytes:
        raise AssetError(
            f"{url} returned {len(resp.content)} bytes, expected at least {min_bytes}"
        )
    # Write through a temp file: a render that starts while this one is only
    # half-written would load a truncated library and fail confusingly.
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(resp.content)
    tmp.replace(dest)
    _log.info("cached render asset %s (%d bytes)", dest.name, len(resp.content))
