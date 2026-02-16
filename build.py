# /// script
# dependencies = ["pillow", "jinja2", "httpx"]
# ///
"""
Flashbulb: Build a static photo archive from a Flickr data export.

Usage:
    uv run --script build.py

Expects zip files from Flickr export in the current directory.
Outputs a static site to ./public_html/
"""

import html
import json
import glob
import os
import re
import shutil
import zipfile
from pathlib import Path
from datetime import datetime

from PIL import Image
from jinja2 import Environment
from markupsafe import Markup

_jinja_env = Environment(autoescape=True)
Template = _jinja_env.from_string

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SITE_DIR = Path("public_html")
THUMB_SIZE = 320    # square crop (2x for 160px grid cells)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXTENSIONS = {".3gp", ".avi", ".mp4", ".mov"}

# ---------------------------------------------------------------------------
# Step 1: Extract zips
# ---------------------------------------------------------------------------

def extract_zips(work_dir: Path):
    """Extract all zips into work_dir, skipping if already done."""
    if work_dir.exists() and any(work_dir.iterdir()):
        print(f"  {work_dir} already exists, skipping extraction")
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    for zf in sorted(glob.glob("*.zip")):
        print(f"  Extracting {zf}...")
        with zipfile.ZipFile(zf) as z:
            z.extractall(work_dir)


# ---------------------------------------------------------------------------
# Step 2: Parse metadata
# ---------------------------------------------------------------------------

def load_photos(work_dir: Path) -> list[dict]:
    """Load all photo_*.json files, merge aggregate comments, return sorted list."""

    # Load NSID -> display name mapping (scraped from Flickr profile pages)
    nsid_names: dict[str, str] = {}
    nsid_file = work_dir / "nsid_names.json"
    if nsid_file.exists():
        with open(nsid_file) as f:
            nsid_names = json.load(f)
        print(f"  Loaded {len(nsid_names)} NSID name mappings")

    # Load aggregate comments file and index by photo_id
    agg_comments: dict[str, list[dict]] = {}
    agg_file = work_dir / "photos_comments_part001.json"
    if agg_file.exists():
        with open(agg_file) as f:
            agg_data = json.load(f)
        for c in agg_data.get("comments", []):
            agg_comments.setdefault(c["photo_id"], []).append(c)
        print(f"  Loaded {sum(len(v) for v in agg_comments.values())} aggregate comments")

    photos = []
    for jf in sorted(work_dir.glob("photo_*.json")):
        with open(jf) as f:
            data = json.load(f)

        # Note: including all photos regardless of privacy setting

        # Merge aggregate comments that aren't already in the inline list.
        # Inline comments have {id, date, user, comment, url}.
        # Aggregate comments have {photo_id, photo_url, comment, comment_url, created}.
        inline = data.get("comments", [])
        inline_texts = {c.get("comment", "") for c in inline}
        for ac in agg_comments.get(data["id"], []):
            if ac.get("comment", "") not in inline_texts:
                inline.append({
                    "id": "",
                    "date": ac.get("created", ""),
                    "user": "commenter",
                    "comment": ac["comment"],
                    "url": ac.get("comment_url", ""),
                })
        data["comments"] = inline

        # Flickr stores descriptions as HTML (with <a>, <b>, <i> tags and
        # HTML entities like &quot;). Unescape the entities and mark as
        # safe Markup so Jinja2 autoescape passes the HTML through as-is.
        desc = data.get("description", "")
        if desc:
            data["description"] = Markup(html.unescape(desc))

        # Comments from Flickr also contain HTML entities; resolve NSIDs to names
        for c in data.get("comments", []):
            if c.get("comment"):
                c["comment"] = Markup(html.unescape(c["comment"]))
            if c.get("user") and c["user"] in nsid_names:
                c["user"] = nsid_names[c["user"]]

        photos.append(data)

    # Sort by date_taken descending (newest first)
    photos.sort(key=lambda p: p.get("date_taken", ""), reverse=True)
    print(f"  Loaded {len(photos)} photos")
    return photos


# ---------------------------------------------------------------------------
# Step 3: Match image files to metadata
# ---------------------------------------------------------------------------

def build_file_index(work_dir: Path) -> dict[str, Path]:
    """Map flickr photo IDs to their actual files on disk.

    Filenames look like: {slug}_{id}_o.{ext} or {slug}_{id}.{ext} (videos)
    """
    index = {}
    for f in work_dir.iterdir():
        if f.suffix.lower() == ".json":
            continue
        # Try to extract the flickr ID from the filename
        # Pattern: anything_{digits}_o.ext or anything_{digits}.ext
        m = re.search(r"_(\d+)(?:_o)?\.(\w+)$", f.name)
        if m:
            photo_id = m.group(1)
            index[photo_id] = f
    print(f"  Indexed {len(index)} media files")
    return index


# ---------------------------------------------------------------------------
# Step 4: Generate resized images
# ---------------------------------------------------------------------------

def make_thumbnail(src: Path, dst: Path):
    """Create a square center-crop thumbnail."""
    with Image.open(src) as img:
        img = img.convert("RGB")

        # Apply rotation if needed
        # (Pillow handles EXIF orientation automatically with .convert())

        # Center crop to square
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        img.save(dst, "JPEG", quality=80)


def process_media(photos: list[dict], file_index: dict[str, Path]):
    """Generate thumbnails for each photo and copy originals."""
    photos_dir = SITE_DIR / "photos"
    total = len(photos)

    for i, photo in enumerate(photos):
        pid = photo["id"]
        src = file_index.get(pid)
        if not src:
            print(f"  [{i+1}/{total}] MISSING file for {pid}: {photo['name']}")
            photo["_has_file"] = False
            continue

        photo["_has_file"] = True
        photo["_src_path"] = src
        photo["_ext"] = src.suffix.lower()
        photo["_is_video"] = photo["_ext"] in VIDEO_EXTENSIONS
        photo["_is_image"] = photo["_ext"] in IMAGE_EXTENSIONS

        out_dir = photos_dir / pid
        out_dir.mkdir(parents=True, exist_ok=True)

        # Copy original
        original_dst = out_dir / f"original{photo['_ext']}"
        if not original_dst.exists():
            shutil.copy2(src, original_dst)
        photo["_original_filename"] = original_dst.name

        # Generate thumbnail
        thumb_dst = out_dir / "thumb.jpg"

        if photo["_is_image"]:
            if not thumb_dst.exists():
                try:
                    make_thumbnail(src, thumb_dst)
                except Exception as e:
                    print(f"  [{i+1}/{total}] Thumb failed for {pid}: {e}")

        elif photo["_is_video"] and not thumb_dst.exists():
            # Download Flickr's poster frame and make a thumbnail from it
            poster_url = photo.get("original", "")
            if poster_url:
                try:
                    import httpx
                    poster_dst = out_dir / "poster.jpg"
                    if not poster_dst.exists():
                        print(f"  [{i+1}/{total}] Downloading poster for video {pid}...")
                        resp = httpx.get(poster_url, follow_redirects=True, timeout=30)
                        resp.raise_for_status()
                        poster_dst.write_bytes(resp.content)
                    make_thumbnail(poster_dst, thumb_dst)
                except Exception as e:
                    print(f"  [{i+1}/{total}] Video poster failed for {pid}: {e}")

        if (i + 1) % 100 == 0 or i + 1 == total:
            print(f"  [{i+1}/{total}] processed")


# ---------------------------------------------------------------------------
# Step 5: Generate HTML
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Shared CSS (written to assets/style.css)
# ---------------------------------------------------------------------------

SHARED_CSS = """\
/* ── reset & base ── */
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  font-family: "Inter", "SF Pro Text", system-ui, -apple-system, sans-serif;
  font-size: 15px; line-height: 1.6;
  background: #0e0e0e; color: #c8c8c8;
  -webkit-font-smoothing: antialiased;
}
a { color: #7db8e0; text-decoration: none; transition: color 0.15s; }
a:hover { color: #aed4f0; }
h1 { font-size: 1.5em; font-weight: 500; letter-spacing: -0.01em; margin: 0 0 4px; }
h2 { font-size: 1.1em; font-weight: 500; color: #999; margin: 0 0 8px; }
.subtitle { font-size: 0.88em; color: #777; margin-bottom: 24px; line-height: 1.5; }
.subtitle a { color: #888; }
.subtitle a:hover { color: #aaa; }

/* ── photo grid ── */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 3px;
}
.grid a {
  display: block; aspect-ratio: 1; overflow: hidden;
  border-radius: 2px; position: relative;
}
.grid img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform 0.25s ease, filter 0.25s ease;
}
.grid a:hover img { transform: scale(1.05); filter: brightness(1.15); }
.grid .video-badge::after {
  content: "\\25B6"; position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  color: rgba(255,255,255,0.9); font-size: 1.8em;
  text-shadow: 0 1px 6px rgba(0,0,0,0.6); pointer-events: none;
}

/* ── tag pills ── */
.tags { margin: 12px 0; display: flex; flex-wrap: wrap; gap: 4px; }
.tags a {
  display: inline-block; background: #1a1a1a; padding: 3px 10px;
  border-radius: 12px; font-size: 0.82em; color: #aaa;
  border: 1px solid #2a2a2a; transition: background 0.15s, color 0.15s;
}
.tags a:hover { background: #252525; color: #ddd; border-color: #3a3a3a; }

/* ── year sections on index ── */
.year-section { margin-bottom: 36px; }
.year-header {
  font-size: 1.05em; font-weight: 500; color: #888;
  margin: 0 0 10px; padding: 10px 0 6px;
  border-bottom: 1px solid #222;
  position: sticky; top: 0; background: #0e0e0e; z-index: 10;
}
.year-header span { font-size: 0.75em; color: #555; font-weight: 400; margin-left: 6px; }

/* ── table of contents ── */
.toc { margin-bottom: 24px; font-size: 0.88em; display: flex; flex-wrap: wrap; gap: 4px 14px; }
.toc a { color: #666; }
.toc a:hover { color: #aaa; }

/* ── photo page ── */
.photo-page { max-width: 1200px; margin: 0 auto; }
.nav {
  margin-bottom: 20px; padding-bottom: 12px;
  border-bottom: 1px solid #1a1a1a;
  font-size: 0.88em; display: flex; gap: 16px; flex-wrap: wrap;
}
.nav a { color: #666; }
.nav a:hover { color: #aaa; }
.date { color: #666; font-size: 0.88em; margin-top: 2px; }
.media { margin: 20px 0; }
.media img {
  max-width: 100%; max-height: 82vh; display: block;
  border-radius: 3px;
}
.media video {
  max-width: 100%; max-height: 82vh; display: block;
  border-radius: 3px;
}
.description { margin: 16px 0; line-height: 1.65; color: #b0b0b0; }
.comments { margin-top: 24px; }
.comments > strong { font-size: 0.9em; color: #888; font-weight: 500; }
.comment {
  background: #161616; padding: 10px 14px; margin: 8px 0;
  border-radius: 6px; font-size: 0.88em; line-height: 1.5;
  border-left: 2px solid #2a2a2a;
}
.comment .comment-date { color: #555; font-size: 0.82em; margin-top: 4px; }
.notes { margin-top: 12px; font-size: 0.85em; color: #888; line-height: 1.5; }
.meta { font-size: 0.82em; color: #555; margin-top: 20px; line-height: 1.6; }
.meta a { color: #666; }
.meta a:hover { color: #999; }
.original-link { font-size: 0.82em; margin-top: 10px; }
.original-link a { color: #666; }
.original-link a:hover { color: #999; }
.stats {
  font-size: 0.82em; color: #555; margin-top: 32px;
  padding-top: 16px; border-top: 1px solid #1a1a1a;
}

/* ── tag index ── */
.tag-list { line-height: 2.2; }
.tag-list a {
  display: inline-block; background: #161616; padding: 3px 12px; margin: 3px;
  border-radius: 12px; color: #aaa;
  border: 1px solid #222; transition: background 0.15s, color 0.15s;
}
.tag-list a:hover { background: #1e1e1e; color: #ddd; border-color: #333; }
.tag-list .count { color: #555; font-size: 0.82em; }

/* ── search ── */
.search-box { margin-bottom: 24px; }
.search-box input {
  width: 100%; max-width: 420px; padding: 10px 14px; font-size: 0.95em;
  background: #161616; color: #c8c8c8; border: 1px solid #2a2a2a; border-radius: 8px;
  font-family: inherit; transition: border-color 0.15s;
}
.search-box input::placeholder { color: #555; }
.search-box input:focus { outline: none; border-color: #4a7a9a; }
#search-results { margin-bottom: 24px; }
#search-results .result-count { font-size: 0.82em; color: #666; margin-bottom: 10px; }

/* ── responsive ── */
@media (max-width: 640px) {
  body { padding: 14px; }
  h1 { font-size: 1.3em; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 2px; }
  .toc { gap: 2px 10px; font-size: 0.82em; }
  .nav { gap: 10px; font-size: 0.82em; }
  .search-box input { max-width: 100%; }
  .media img, .media video { max-height: 60vh; }
}
@media (min-width: 1400px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); }
}
"""

SEARCH_JS = """\
(function() {
  var input = document.getElementById('search-input');
  var results = document.getElementById('search-results');
  var yearSections = document.querySelectorAll('.year-section');
  var toc = document.querySelector('.toc');

  // Build index from data attributes already in the DOM
  var items = Array.from(document.querySelectorAll('.year-section .grid a')).map(function(a) {
    return {
      el: a,
      haystack: (
        (a.title || '') + ' ' +
        (a.dataset.desc || '') + ' ' +
        (a.dataset.tags || '').replace(/,/g, ' ') + ' ' +
        (a.dataset.date || '')
      ).toLowerCase()
    };
  });

  input.addEventListener('input', function() {
    var q = input.value.trim().toLowerCase();
    if (!q) {
      results.innerHTML = '';
      results.style.display = 'none';
      yearSections.forEach(function(s) { s.style.display = ''; });
      if (toc) toc.style.display = '';
      return;
    }

    yearSections.forEach(function(s) { s.style.display = 'none'; });
    if (toc) toc.style.display = 'none';
    results.style.display = '';

    var terms = q.split(/\\s+/);
    var matches = items.filter(function(item) {
      return terms.every(function(t) { return item.haystack.indexOf(t) !== -1; });
    });

    var html = '<div class="result-count">' + matches.length + ' results for &ldquo;' +
      q.replace(/</g,'&lt;') + '&rdquo;</div><div class="grid">';
    matches.forEach(function(item) {
      html += item.el.outerHTML;
    });
    html += '</div>';
    results.innerHTML = html;
  });
})();
"""

INDEX_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flashbulb — Adam Smith's Photo Archive</title>
<link rel="icon" href="https://adamsmith.as/favicon.ico">
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<h1>Flashbulb</h1>
<p class="subtitle">{{ photo_count }} photos, {{ year_min }}&ndash;{{ year_max }} &middot; Adam Smith's photo archive &middot; <a href="tags/">tags</a> &middot; <a href="https://github.com/rndmcnlly/flashbulb">source</a></p>
<div class="search-box"><input type="text" id="search-input" placeholder="Search titles, tags, descriptions..."></div>
<div id="search-results" style="display:none"></div>
<div class="toc">{% for year, photos in years %}<a href="#y{{ year }}">{{ year }} ({{ photos|length }})</a>{% endfor %}</div>
{% for year, photos in years %}
<div class="year-section" id="y{{ year }}">
<h2 class="year-header">{{ year }} <span>{{ photos|length }} photos</span></h2>
<div class="grid">
{% for photo in photos %}<a href="photos/{{ photo.id }}/" title="{{ photo.name }}" data-tags="{{ photo.tags|map(attribute='tag')|join(',') }}" data-desc="{{ photo.description|default('',true)|striptags|truncate(200,true,'') }}" data-date="{{ photo.date_taken }}"{% if photo._is_video %} class="video-badge"{% endif %}><img src="photos/{{ photo.id }}/thumb.jpg" alt="" loading="lazy"></a>
{% endfor %}
</div>
</div>
{% endfor %}
<div class="stats">
  Exported from Flickr. {{ tag_count }} tags across {{ tagged_count }} photos.
  {{ comment_count }} comments on {{ commented_count }} photos.
  {{ geo_count }} geotagged.
</div>
<script src="assets/search.js"></script>
</body>
</html>
""")

PHOTO_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ photo.name }} — Flashbulb</title>
<link rel="icon" href="https://adamsmith.as/favicon.ico">
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body class="photo-page">
<div class="nav">
  <a href="../../">&larr; all photos</a>
  <a href="../../tags/">tags</a>
  {% if prev_id %}<a href="../{{ prev_id }}/">&larr; prev</a>{% endif %}
  {% if next_id %}<a href="../{{ next_id }}/">next &rarr;</a>{% endif %}
</div>

<h1>{{ photo.name }}</h1>
<div class="date">{{ photo.date_taken }}</div>

<div class="media">
{% if photo._is_video %}
  <video controls preload="metadata">
    <source src="original{{ photo._ext }}">
    Your browser doesn't support this video format.
  </video>
{% else %}
  <img src="original{{ photo._ext }}" alt="{{ photo.name }}">
{% endif %}
</div>

{% if photo.description %}<div class="description">{{ photo.description }}</div>{% endif %}

{% if photo.tags %}<div class="tags">{% for t in photo.tags %}<a href="../../tags/{{ t.tag }}/">{{ t.tag }}</a>{% endfor %}</div>{% endif %}

{% if photo.notes %}
<div class="notes">
  <strong>Notes:</strong>
  {% for n in photo.notes %}
    <em>"{{ n.text }}"</em>{% if not loop.last %}, {% endif %}
  {% endfor %}
</div>
{% endif %}

{% if photo.geo %}
<div class="meta">
  {% set g = photo.geo[0] %}
  lat {{ "%.4f"|format(g.latitude|float / 1000000) }}, lon {{ "%.4f"|format(g.longitude|float / 1000000) }}
</div>
{% endif %}

{% if photo.comments %}
<div class="comments">
  <strong>Comments ({{ photo.comments|length }}):</strong>
  {% for c in photo.comments %}
  <div class="comment">
    <div>{{ c.comment }}</div>
    <div class="comment-date">{{ c.date }} &middot; {{ c.user }}</div>
  </div>
  {% endfor %}
</div>
{% endif %}

<div class="meta">
  {{ photo.count_views }} views &middot; {{ photo.count_faves }} faves &middot; {{ photo.license }}
  <br><a href="{{ photo.photopage }}">Original Flickr page</a>
</div>

{% if photo._is_video %}
<div class="original-link">
  <a href="original{{ photo._ext }}" download>Download original video</a>
</div>
{% endif %}

</body>
</html>
""")

TAG_INDEX_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tags — Flashbulb</title>
<link rel="icon" href="https://adamsmith.as/favicon.ico">
<link rel="stylesheet" href="../assets/style.css">
</head>
<body>
<div class="nav"><a href="../">&larr; all photos</a></div>
<h1>Tags</h1>
<p class="subtitle">{{ tags|length }} tags across {{ photo_count }} tagged photos</p>
<div class="tag-list">
{% for tag, count in tags %}<a href="{{ tag }}/">{{ tag }} <span class="count">({{ count }})</span></a>{% endfor %}
</div>
</body>
</html>
""")

TAG_PAGE_TEMPLATE = Template("""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ tag }} — Flashbulb</title>
<link rel="icon" href="https://adamsmith.as/favicon.ico">
<link rel="stylesheet" href="../../assets/style.css">
</head>
<body>
<div class="nav"><a href="../../">&larr; all photos</a> <a href="../">all tags</a></div>
<h1>{{ tag }}</h1>
<p class="subtitle">{{ photos|length }} photos</p>
<div class="grid">
{% for photo in photos %}<a href="../../photos/{{ photo.id }}/" title="{{ photo.name }}"{% if photo._is_video %} class="video-badge"{% endif %}><img src="../../photos/{{ photo.id }}/thumb.jpg" alt="" loading="lazy"></a>
{% endfor %}
</div>
</body>
</html>
""")


def generate_html(photos: list[dict]):
    """Generate index.html, per-photo pages, tag pages, and shared assets."""
    from collections import OrderedDict

    # Filter to photos with files
    visible = [p for p in photos if p.get("_has_file")]

    # Write shared CSS
    assets_dir = SITE_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "style.css").write_text(SHARED_CSS)
    (assets_dir / "search.js").write_text(SEARCH_JS)
    print(f"  Wrote assets/style.css, assets/search.js")

    # Gather stats
    years = [p["date_taken"][:4] for p in visible if p.get("date_taken")]
    year_min = min(years) if years else "?"
    year_max = max(years) if years else "?"

    # Build tag -> photos mapping
    tag_photos: dict[str, list[dict]] = {}
    for p in visible:
        for t in p.get("tags", []):
            tag_photos.setdefault(t["tag"], []).append(p)

    # Group photos by year (newest first)
    by_year = OrderedDict()
    for p in visible:
        yr = p.get("date_taken", "")[:4] or "unknown"
        by_year.setdefault(yr, []).append(p)

    # Write index
    index_html = INDEX_TEMPLATE.render(
        years=list(by_year.items()),
        photo_count=len(visible),
        year_min=year_min,
        year_max=year_max,
        tag_count=len(tag_photos),
        tagged_count=sum(1 for p in visible if p.get("tags")),
        comment_count=sum(len(p.get("comments", [])) for p in visible),
        commented_count=sum(1 for p in visible if p.get("comments")),
        geo_count=sum(1 for p in visible if p.get("geo")),
    )
    (SITE_DIR / "index.html").write_text(index_html)
    print(f"  Wrote index.html ({len(visible)} photos)")

    # Write per-photo pages
    for i, photo in enumerate(visible):
        prev_id = visible[i - 1]["id"] if i > 0 else None
        next_id = visible[i + 1]["id"] if i < len(visible) - 1 else None

        photo_html = PHOTO_TEMPLATE.render(
            photo=photo,
            prev_id=prev_id,
            next_id=next_id,
        )
        out_dir = SITE_DIR / "photos" / photo["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(photo_html)

    print(f"  Wrote {len(visible)} photo pages")

    # Write tag index
    tags_dir = SITE_DIR / "tags"
    tags_dir.mkdir(parents=True, exist_ok=True)
    sorted_tags = sorted(tag_photos.items(), key=lambda x: -len(x[1]))
    tag_index_html = TAG_INDEX_TEMPLATE.render(
        tags=[(t, len(ps)) for t, ps in sorted_tags],
        photo_count=sum(1 for p in visible if p.get("tags")),
    )
    (tags_dir / "index.html").write_text(tag_index_html)
    print(f"  Wrote tags/index.html ({len(sorted_tags)} tags)")

    # Write per-tag pages
    for tag, photos_for_tag in sorted_tags:
        tag_dir = tags_dir / tag
        tag_dir.mkdir(parents=True, exist_ok=True)
        tag_html = TAG_PAGE_TEMPLATE.render(
            tag=tag,
            photos=photos_for_tag,
        )
        (tag_dir / "index.html").write_text(tag_html)

    print(f"  Wrote {len(sorted_tags)} tag pages")

    # (search data is now embedded as data attributes in index.html grid links)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    work_dir = Path("_extracted")
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    print("Step 1: Extracting zips...")
    extract_zips(work_dir)

    print("Step 2: Loading metadata...")
    photos = load_photos(work_dir)

    print("Step 3: Building file index...")
    file_index = build_file_index(work_dir)

    print("Step 4: Processing media (thumbnails + medium)...")
    process_media(photos, file_index)

    print("Step 5: Generating HTML...")
    generate_html(photos)

    print(f"\nDone! Site written to {SITE_DIR}/")
    print(f"Run: python3 -m http.server -d {SITE_DIR} 8000")


if __name__ == "__main__":
    main()
