# Zmanim Documentation

User-facing documentation for KosherJava zmanim, generated from raw Javadoc and built with Zensical.

## Generate Docs
```powershell
# Pull changes from KosherJava
git subtree pull --prefix=kosher-java https://github.com/KosherJava/zmanim master  --squash
# Parse Javadoc
uv run python tools/parse-javadoc.py
# Generate Docs to generated-docs.json
uv run python tools/generate-docs.py generate-json
# Render Markdown to docs/
uv run python tools/generate-docs.py render-md
# Deploy with mike
uv run mike deploy latest
# Push to GitHub
git push origin gh-pages
```
## Generation Rules

The generated explanations are intentionally limited to the raw Javadoc stored in `docs.raw`. The parser does not split the Javadoc into summary, return, link, or tag fields; it passes the raw block through so the GPT model can rewrite source-backed text into clearer user-facing prose. Deprecated zmanim remain included with warnings, and settings such as offsets or elevation are documented only when the raw Javadoc supports them.

Categories are controlled in `tools/zman_categories.py`. Edit `CATEGORY_LABELS` to rename categories, `CATEGORY_ORDER` to reorder them, and `ZMAN_CATEGORIES` to move a zman to a different category. Any generated zman that is not listed in `ZMAN_CATEGORIES` or `EXCLUDED_ZMANIM` is treated as an error.
