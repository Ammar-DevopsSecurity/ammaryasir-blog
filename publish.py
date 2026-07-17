#!/usr/bin/env python3
"""
publish.py — Daniyal Ahmed Blog Publisher
==========================================
Drop your .md posts in:   D:/obsidianf/blogs/
Images are found AUTOMATICALLY — wherever Obsidian saved them:
  • D:/obsidianf/blogs/img/        (manual img folder)
  • D:/obsidianf/blogs/            (same folder as post)
  • D:/obsidianf/Attachements/     (Obsidian attachments folder)
  • D:/obsidianf/                  (vault root — pasted images land here)
  • Recursive search entire vault  (last resort)

Run this script and it does everything:
  1. Scans post for ALL image references (wiki + markdown style)
  2. Hunts each image across all Obsidian locations above
  3. Copies found images to Hugo static/img/posts/
  4. Fixes image paths (Obsidian → Hugo format)
  5. Adds proper Hugo front matter if missing
  6. Builds the site with Hugo
  7. Git add + commit + push

Usage:
  python publish.py              # publish all new/changed posts
  python publish.py --dry-run   # preview what would happen, no changes
  python publish.py --post "My Post Title.md"  # publish one specific post
"""

import os
import re
import sys
import shutil
import hashlib
import argparse
import subprocess
from urllib.parse import unquote, quote
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output so Unicode box-drawing chars and icons work on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ───────────────────────────────────────────────────────────────────
OBSIDIAN_VAULT   = Path("C:/Users/AR Leptop/Documents/ammaryasir-vault/ammaryasir-vault")
OBSIDIAN_BLOGS   = OBSIDIAN_VAULT / "blogs"
HUGO_ROOT        = Path("C:/Users/AR Leptop/ammaryasir-blog")
HUGO_POSTS       = HUGO_ROOT / "content" / "posts"
HUGO_IMG         = HUGO_ROOT / "static" / "img" / "posts"
HUGO_IMG_URL     = "/img/posts"          # root domain, no subpath needed

# Ordered list of folders to search for images — first match wins
IMAGE_SEARCH_DIRS = [
    OBSIDIAN_BLOGS / "img",          # explicit img folder next to posts
    OBSIDIAN_BLOGS,                  # same folder as the post
    OBSIDIAN_VAULT / "Attachements", # Obsidian default attachments (note: typo in your vault)
    OBSIDIAN_VAULT / "Attachments",  # also try correct spelling
    OBSIDIAN_VAULT,                  # vault root — where Ctrl+V pasted images go
]
# ─────────────────────────────────────────────────────────────────────────────


def log(msg, level="INFO"):
    icons = {"INFO": "→", "OK": "✓", "WARN": "⚠", "ERR": "✗", "SKIP": "○"}
    print(f"  {icons.get(level, '·')} {msg}")


def file_hash(path: Path) -> str:
    """MD5 of file contents for change detection."""
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def slugify(name: str) -> str:
    """Turn a filename into a URL-safe slug."""
    slug = name.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug


def extract_first_heading(content: str) -> str | None:
    """Return the text of the first # H1 heading in the document, or None."""
    m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else None


def ensure_frontmatter(content: str, source_path: Path) -> str:
    """
    If the markdown has no Hugo front matter, add a sensible default.
    Title priority: first # Heading in file  >  filename stem.
    Handles both YAML (---) and TOML (+++) front matter.
    """
    stripped = content.lstrip()
    if stripped.startswith("---") or stripped.startswith("+++"):
        # Already has front matter — just make sure draft = false
        content = re.sub(r"(draft\s*[=:]\s*)(true)", r"\1false", content)
        return content

    # No front matter — pick title from first heading, else filename
    heading = extract_first_heading(content)
    title   = heading if heading else source_path.stem.replace("-", " ").replace("_", " ").title()
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = f"""---
title: "{title}"
date: {now}
draft: false
tags: []
categories: []
---

"""
    return fm + content


def find_image_in_vault(fname: str) -> Path | None:
    """
    Hunt for an image file across all known Obsidian locations.
    Handles URL-encoded names (spaces as %20) and case-insensitive match
    on Windows as fallback.

    Search order:
      1. blogs/img/          — manual img folder
      2. blogs/              — same folder as the post
      3. vault/Attachements/ — Obsidian attachments (your vault's typo)
      4. vault/Attachments/  — correct spelling fallback
      5. vault root/         — Ctrl+V pasted images land here
      6. Recursive vault     — last resort, full scan
    """
    # Decode URL-encoded names e.g. "Pasted%20image.png" → "Pasted image.png"
    decoded = unquote(fname)

    candidates = [decoded, fname]  # try decoded first, then original

    for search_dir in IMAGE_SEARCH_DIRS:
        if not search_dir.exists():
            continue
        for name in candidates:
            p = search_dir / name
            if p.exists():
                return p

    # Last resort: walk the entire vault (slow but thorough)
    for name in candidates:
        for found in OBSIDIAN_VAULT.rglob(name):
            # Skip hidden folders and Hugo output
            parts = found.parts
            if any(p.startswith(".") for p in parts):
                continue
            return found

    return None


def apply_captions(content: str) -> str:
    """
    Convert <<caption text>> written after an image into a markdown title
    attribute so the render hook shows it as a <figcaption>.

    Works on same line OR the line immediately after the image.

    Patterns:
      ![alt](url)<<My caption>>
      ![alt](url)
      <<My caption>>

      ![[image.png]]<<My caption>>
      ![[image.png]]
      <<My caption>>

    Wiki-style captions are stored as ![[image.png||caption]] so
    fix_image_paths can pick them up.
    """
    def clean_caption(raw: str) -> str:
        """Strip surrounding whitespace and accidental quote chars from caption."""
        return raw.strip().strip('"').strip("'").strip()

    # 1. Standard markdown image + <<caption>>
    def std_caption(m):
        alt     = m.group(1)
        src     = m.group(2).strip()
        caption = clean_caption(m.group(3))
        src_clean = re.sub(r'\s+"[^"]*"$', "", src).strip()  # drop existing title
        return f'![{alt}]({src_clean} "{caption}")'

    content = re.sub(
        r'!\[([^\]]*)\]\(([^)]+)\)\s*\n?\s*<<([^>]+)>>',
        std_caption,
        content,
    )

    # 2. Wiki-style image + <<caption>> — store caption with || marker
    def wiki_caption(m):
        inner   = m.group(1).strip().split("|")[0].strip()  # drop size hints
        caption = clean_caption(m.group(2))
        return f"![[{inner}||{caption}]]"

    content = re.sub(
        r'!\[\[([^\]]+)\]\]\s*\n?\s*<<([^>]+)>>',
        wiki_caption,
        content,
    )

    return content


def fix_image_paths(content: str) -> tuple[str, list[str]]:
    """
    Convert Obsidian image references to Hugo static paths.

    Handles:
      Obsidian wiki-embed:  ![[image.png]]
                            ![[Pasted image 20241227081110.png]]
                            ![[image.png||caption]]   ← from apply_captions
      Standard markdown:    ![alt](img/image.png)
                            ![alt](image.png)
                            ![alt](./img/image.png)
                            ![alt](Pasted%20image%2020241227.png)

    Returns (new_content, list_of_original_filenames_referenced).
    """
    images_found = []

    # 1. Obsidian wiki-style: ![[filename.ext]] or ![[filename.ext||caption]]
    def replace_wiki(m):
        raw = m.group(1).strip()

        # Extract caption if present (our || marker from apply_captions)
        caption = None
        if "||" in raw:
            raw, caption = raw.split("||", 1)
            caption = caption.strip()

        # Strip Obsidian resize hint e.g. ![[img.png|300]]
        fname = raw.split("|")[0].strip()
        images_found.append(fname)
        url_name = quote(Path(fname).name)

        if caption:
            return f'![{Path(fname).name}]({HUGO_IMG_URL}/{url_name} "{caption}")'
        return f"![{Path(fname).name}]({HUGO_IMG_URL}/{url_name})"

    content = re.sub(r"!\[\[([^\]]+)\]\]", replace_wiki, content)

    # 2. Standard markdown images — rewrite src to /img/posts/...
    IMG_EXTS = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|bmp|tiff?)$", re.IGNORECASE)

    def replace_md(m):
        alt = m.group(1)
        src = m.group(2).strip()
        # Skip external URLs
        if src.startswith("http://") or src.startswith("https://"):
            return m.group(0)
        fname = Path(unquote(src)).name   # decode first to get real filename
        if fname and IMG_EXTS.search(fname):
            images_found.append(fname)
            return f"![{alt}]({HUGO_IMG_URL}/{quote(fname)})"
        return m.group(0)

    content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace_md, content)

    return content, images_found


def copy_images(image_names: list[str], dry_run: bool) -> list[str]:
    """
    Find each image anywhere in the Obsidian vault and copy to Hugo static/img/posts/.
    Prints clearly where each image was found (or warns if missing).
    """
    copied  = []
    missing = []

    if not dry_run:
        HUGO_IMG.mkdir(parents=True, exist_ok=True)

    for fname in dict.fromkeys(image_names):  # dedup, preserve order
        dst = HUGO_IMG / Path(fname).name

        src = find_image_in_vault(fname)

        if src is None:
            missing.append(fname)
            continue

        rel = src.relative_to(OBSIDIAN_VAULT) if src.is_relative_to(OBSIDIAN_VAULT) else src

        if dry_run:
            log(f"[DRY] Found '{fname}' at vault/{rel} → would copy to Hugo", "INFO")
        else:
            shutil.copy2(src, dst)
            copied.append(fname)
            log(f"Copied '{Path(fname).name}'  ← vault/{rel}", "OK")

    for fname in missing:
        log(f"Image NOT found anywhere in vault: {fname}", "WARN")
        log(f"  Put it in: {OBSIDIAN_BLOGS / 'img' / fname}", "WARN")

    return copied


def publish_post(md_path: Path, dry_run: bool):
    """Process and publish a single markdown post."""
    slug        = slugify(md_path.stem)
    dest_path   = HUGO_POSTS / f"{slug}.md"

    content = md_path.read_text(encoding="utf-8")
    content = ensure_frontmatter(content, md_path)
    content = apply_captions(content)       # <<caption>> → title attr
    content, images = fix_image_paths(content)

    if images:
        copy_images(images, dry_run)

    if dry_run:
        log(f"[DRY] Would write: content/posts/{slug}.md  (images: {images or 'none'})", "INFO")
        return

    HUGO_POSTS.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    log(f"Published: content/posts/{slug}.md", "OK")


def find_posts_to_publish(only: str | None = None) -> list[Path]:
    """Return list of .md files from obsidian blogs folder."""
    if not OBSIDIAN_BLOGS.exists():
        log(f"Obsidian blogs folder not found: {OBSIDIAN_BLOGS}", "ERR")
        sys.exit(1)

    if only:
        target = OBSIDIAN_BLOGS / only
        if not target.exists():
            log(f"Post not found: {target}", "ERR")
            sys.exit(1)
        return [target]

    posts = sorted(OBSIDIAN_BLOGS.glob("*.md"))
    if not posts:
        log("No .md files found in obsidian blogs folder.", "WARN")
    return posts


def run(cmd: list[str], cwd: Path, label: str, dry_run: bool) -> bool:
    """Run a shell command, return True on success."""
    if dry_run:
        log(f"[DRY] Would run: {' '.join(cmd)}", "INFO")
        return True
    log(f"Running: {' '.join(cmd)}", "INFO")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        log(f"{label} failed:\n    {output}", "ERR")
        return False
    return True


def hugo_build(dry_run: bool) -> bool:
    return run(["hugo", "--minify"], HUGO_ROOT, "Hugo build", dry_run)


def git_has_changes(dry_run: bool) -> bool:
    """Return True if there is anything staged or unstaged to commit."""
    if dry_run:
        return True
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=HUGO_ROOT, capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def git_publish(dry_run: bool, commit_msg: str) -> bool:
    ok = True
    ok = ok and run(["git", "add", "."], HUGO_ROOT, "git add", dry_run)

    if git_has_changes(dry_run):
        ok = ok and run(["git", "commit", "-m", commit_msg], HUGO_ROOT, "git commit", dry_run)
    else:
        log("Nothing new to commit — skipping commit step.", "SKIP")

    ok = ok and run(["git", "pull", "--rebase", "origin", "main"], HUGO_ROOT, "git pull",  dry_run)
    ok = ok and run(["git", "push"],                               HUGO_ROOT, "git push",  dry_run)
    return ok


def remove_post(title: str, dry_run: bool):
    """
    Remove a post from Hugo by title/filename stem.
    Accepts the original post name e.g. 'My Post' or 'My Post.md'
    Deletes the .md from content/posts/ then rebuilds + pushes.
    """
    stem      = Path(title).stem          # strip .md if user included it
    slug      = slugify(stem)
    post_path = HUGO_POSTS / f"{slug}.md"

    # Also try exact stem match in case slugify changes it
    exact_path = HUGO_POSTS / f"{stem}.md"

    target = None
    if post_path.exists():
        target = post_path
    elif exact_path.exists():
        target = exact_path
    else:
        # List what IS there so user can pick the right name
        existing = sorted(HUGO_POSTS.glob("*.md"))
        log(f"Post not found: '{slug}.md'", "ERR")
        if existing:
            log("Posts currently on site:", "INFO")
            for p in existing:
                print(f"       {p.name}")
        sys.exit(1)

    if dry_run:
        log(f"[DRY] Would delete: content/posts/{target.name}", "INFO")
    else:
        target.unlink()
        log(f"Deleted: content/posts/{target.name}", "OK")


def list_posts():
    """Print all posts currently published to Hugo."""
    posts = sorted(HUGO_POSTS.glob("*.md"))
    if not posts:
        print("\n  No posts published yet.\n")
        return
    print(f"\n  Published posts ({len(posts)}):")
    for p in posts:
        print(f"    • {p.stem}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Publish Obsidian blog posts to Hugo site")
    parser.add_argument("--dry-run",  action="store_true", help="Preview only, make no changes")
    parser.add_argument("--post",     type=str, default=None, help="Publish one specific post, e.g. 'My Post.md'")
    parser.add_argument("--remove",   type=str, default=None, help="Remove a post from site, e.g. --remove 'My Post'")
    parser.add_argument("--list",     action="store_true",    help="List all currently published posts")
    parser.add_argument("--rebuild",  action="store_true",    help="Just rebuild + push, no new posts needed")
    parser.add_argument("--no-push",  action="store_true", help="Build and commit but don't git push")
    parser.add_argument("--no-build", action="store_true", help="Skip hugo build step")
    args = parser.parse_args()

    dry = args.dry_run
    if dry:
        print("\n  *** DRY RUN — no files will be changed ***\n")

    print("\n╔══════════════════════════════════════╗")
    print("║   Ammar Bin Yasir — Blog Publisher     ║")
    print("╚══════════════════════════════════════╝\n")

    # ── LIST mode ────────────────────────────────────────────────────────────
    if args.list:
        list_posts()
        sys.exit(0)

    # ── REBUILD mode ─────────────────────────────────────────────────────────
    if args.rebuild:
        log("Rebuild only — no new posts", "INFO")
        print()
        log("Building site with Hugo...", "INFO")
        if not hugo_build(dry):
            log("Hugo build failed.", "ERR")
            sys.exit(1)
        log("Hugo build complete.", "OK")
        print()
        log("Committing and pushing...", "INFO")
        commit_msg = f"rebuild: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        if not git_publish(dry, commit_msg) and not dry:
            log("Git step failed.", "ERR")
            sys.exit(1)
        print()
        print("  ✓ Site rebuilt and pushed.\n")
        sys.exit(0)

    # ── REMOVE mode ──────────────────────────────────────────────────────────
    if args.remove:
        remove_post(args.remove, dry)
        commit_msg = f"posts: remove '{args.remove}' [{datetime.now().strftime('%Y-%m-%d %H:%M')}]"

        if not args.no_build:
            print()
            log("Rebuilding site...", "INFO")
            if not hugo_build(dry):
                log("Hugo build failed.", "ERR")
                sys.exit(1)
            log("Hugo build complete.", "OK")

        print()
        log("Committing and pushing...", "INFO")
        if not git_publish(dry, commit_msg) and not dry:
            log("Git step failed.", "ERR")
            sys.exit(1)
        print()
        print(f"  ✓ Post removed and site updated.\n")
        sys.exit(0)

    # ── PUBLISH mode (default) ────────────────────────────────────────────────
    posts = find_posts_to_publish(args.post)
    log(f"Found {len(posts)} post(s) to process", "INFO")

    if not posts:
        print("\n  Nothing to publish. Add .md files to:")
        print(f"  {OBSIDIAN_BLOGS}\n")
        sys.exit(0)

    for post in posts:
        log(f"Processing: {post.name}", "INFO")
        publish_post(post, dry)

    if not args.no_build:
        print()
        log("Building site with Hugo...", "INFO")
        if not hugo_build(dry):
            log("Hugo build failed — aborting git steps.", "ERR")
            sys.exit(1)
        log("Hugo build complete.", "OK")

    now_str     = datetime.now().strftime("%Y-%m-%d %H:%M")
    post_titles = ", ".join(p.stem for p in posts)
    commit_msg  = f"posts: add/update — {post_titles} [{now_str}]"

    print()
    log("Committing and pushing...", "INFO")
    if not git_publish(dry, commit_msg) and not dry:
        log("Git step failed. Check above for errors.", "ERR")
        sys.exit(1)

    print()
    print("  ✓ All done! Site updated.\n")


if __name__ == "__main__":
    main()