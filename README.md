# Flashbulb

> "I'm done actively using Flickr, and I want to stop making monthly payments for a Pro account. Let's make a permanent self-hosted archive."

A single-file Python script that turns a Flickr data export into a static, self-hosted photo archive website. No database, no server-side code — just HTML, CSS, and JS you can drop on any web host.

**Live example:** [flashbulb.adamsmith.as](https://flashbulb.adamsmith.as) — 1,318 photos from 2002–2014

## Features

- Year-grouped photo grid with sticky headers and table of contents
- Individual photo pages with prev/next navigation
- Descriptions, tags, comments, notes, geolocation, view counts, license info
- Tag index and per-tag gallery pages
- Client-side search across titles, descriptions, tags, and dates
- Video support (.3gp, .avi, .mp4, .mov) with poster thumbnails
- Retina-optimized thumbnails (320×320px square crops for 160px grid cells)
- Lazy loading on all grid images
- Comment author names resolved from Flickr NSIDs (cached locally)

## Usage

1. Request your Flickr export at [flickr.com/account](https://www.flickr.com/account) (look for the "Your Flickr Data" section). Flickr will email you download links for 3–4 ZIP files.

2. Place the ZIP files in this directory and run:

```bash
uv run --script build.py
```

3. Preview locally:

```bash
python3 -m http.server -d public_html 8000
```

4. Deploy `public_html/` to any static web host.

## How It Works

`build.py` is a single-file script with inline dependency declarations (`pillow`, `jinja2`, `httpx`), designed to be run with [`uv run --script`](https://docs.astral.sh/uv/guides/scripts/).

1. Extracts all ZIPs into `_extracted/` (skips if already done)
2. Parses per-photo JSON metadata, unescaping Flickr's HTML entities
3. Resolves comment author NSIDs to display names via `_extracted/nsid_names.json`
4. Matches media files to metadata by extracting Flickr photo IDs from filenames
5. Generates 320px square thumbnails for each image; downloads Flickr poster frames for videos
6. Copies originals into per-photo directories
7. Renders all HTML pages (index, per-photo, tag index, per-tag) with Jinja2
8. Writes shared CSS and client-side search JS

Build time is roughly 5–10 minutes for ~1,300 photos.

## Output Structure

```
public_html/
├── index.html              # Main gallery, year-grouped, with search
├── assets/
│   ├── style.css
│   └── search.js
├── photos/{id}/
│   ├── index.html          # Photo page with metadata
│   ├── thumb.jpg           # 320×320px square crop
│   └── original.{ext}      # Full-resolution original
└── tags/
    ├── index.html          # Tag cloud sorted by frequency
    └── {tag}/index.html    # Per-tag gallery
```

## Flickr Export Quirks

If you're adapting this script, watch out for:

- **Video originals vs. posters**: The `original` URL in Flickr's JSON points to a JPG poster frame on their CDN, but the actual file in the ZIP is .3gp/.avi/.mp4. The script downloads the poster separately for thumbnail generation.
- **HTML in descriptions**: Flickr stores descriptions with inline HTML tags (`<a>`, `<b>`, `<i>`) and HTML entities. These need unescaping before rendering to avoid double-encoding.
- **NSID usernames**: Comment authors are identified by numeric NSIDs (e.g. `55023503@N00`), not display names. The script scrapes Flickr profile pages to resolve these and caches the mapping locally.
- **Date anomalies**: Some photos have impossible EXIF dates. The script falls back to `date_imported` when `date_taken` is clearly wrong.

## License

MIT
