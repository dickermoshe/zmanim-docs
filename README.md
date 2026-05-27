# Zmanim Documentation

User-facing documentation for KosherJava zmanim, generated from raw Javadoc and built with Zensical.

## Generate Docs

The workflow has four steps:

1. Extract raw Javadoc into JSON:

   ```powershell
   uv run python tools/parse-javadoc.py --source "..\kosher-java\src\main\java" --pretty
   ```

   This writes `methods.json`.

2. Generate user-facing content JSON with GPT:

   ```powershell
   uv run python tools/generate-docs.py generate-json
   ```

   This writes `generated-docs.json`. It reads `OPENAI_API_KEY` from the environment, or `OPENAI-KEY` from `tools/.env`. It defaults to `gpt-5.5`; override that with `OPENAI_MODEL` or `--model`. It uses the same seed and low reasoning effort for every run by default; override them with `--seed` and `--reasoning-effort`. It writes a local `docs-generation-report.json` with category counts, skipped items, and any partial generation warnings. Each model batch is scheduled concurrently with `asyncio`.

3. Render Markdown from the generated JSON:

   ```powershell
   uv run python tools/generate-docs.py render-md
   ```

   The renderer writes category pages at the docs root, such as `docs/alos.md` and `docs/astronomical_dawn.md`. It does not write `docs/index.md` or `docs/index.md`; the main index is maintained by hand.

4. Build the site:

   ```powershell
   uv run zensical build --clean
   ```

## Generation Rules

The generated explanations are intentionally limited to the raw Javadoc stored in `docs.raw`. The parser does not split the Javadoc into summary, return, link, or tag fields; it passes the raw block through so the GPT model can rewrite source-backed text into clearer user-facing prose. Deprecated zmanim remain included with warnings, and settings such as offsets or elevation are documented only when the raw Javadoc supports them.

Categories are controlled in `tools/zman_categories.py`. Edit `CATEGORY_LABELS` to rename categories, `CATEGORY_ORDER` to reorder them, and `ZMAN_CATEGORIES` to move a zman to a different category. Any generated zman that is not listed in `ZMAN_CATEGORIES` or `EXCLUDED_ZMANIM` is treated as an error.
