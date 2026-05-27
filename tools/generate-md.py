from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from docs_common import (
    DEFAULT_GENERATED_OUTPUT,
    DEFAULT_INDEX_LINK,
    DEFAULT_INPUT,
    DEFAULT_PAGES_DIR,
    DEFAULT_REPORT,
    GENERATED_MARKER,
    SourceEntry,
    add_missing_raw_skips,
    base_report,
    grouped_entries,
    load_json,
    print_category_summary,
    source_entries,
    validate_category_config,
    write_json,
)


OLD_GENERATED_PAGES_DIR = Path("docs/zmanim")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render zmanim Markdown pages from generated content JSON.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Parsed methods JSON to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--generated",
        type=Path,
        default=DEFAULT_GENERATED_OUTPUT,
        help=f"Generated content JSON to read (default: {DEFAULT_GENERATED_OUTPUT})",
    )
    parser.add_argument(
        "--pages-dir",
        type=Path,
        default=DEFAULT_PAGES_DIR,
        help=f"Directory for generated category Markdown files (default: {DEFAULT_PAGES_DIR})",
    )
    parser.add_argument(
        "--index-link",
        default=DEFAULT_INDEX_LINK,
        help=f"Back link target for generated category pages (default: {DEFAULT_INDEX_LINK})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Generation report JSON to write (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate generated content and Markdown page planning without writing docs.",
    )
    return parser.parse_args()


def markdown_escape(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def paragraph_text(text: str) -> str:
    return re.sub(r"\s+", " ", markdown_escape(text)).strip()


def should_hide_note(entry: SourceEntry, note: str) -> bool:
    normalized = note.lower().rstrip(".")
    if entry.category == "Kidush Levana" and normalized == (
        "if the zman does not occur on this day, the time may not be available"
    ):
        return True
    return False


def category_id_for_label(category_label: str) -> str:
    from docs_common import CATEGORY_LABELS

    for category_id, label in CATEGORY_LABELS.items():
        if label == category_label:
            return category_id
    raise ValueError(f"No category id found for label {category_label!r}.")


def category_page_paths(
    groups: dict[str, list[SourceEntry]],
    generated_by_id: dict[str, dict[str, Any]],
    pages_dir: Path,
) -> dict[str, Path]:
    page_paths_by_category: dict[str, Path] = {}
    for category, entries in groups.items():
        rendered_entries = [entry for entry in entries if entry.id in generated_by_id]
        if rendered_entries:
            page_paths_by_category[category] = (
                pages_dir / f"{category_id_for_label(category)}.md"
            )
    return page_paths_by_category


def render_item(entry: SourceEntry, generated: dict[str, Any]) -> str:
    lines: list[str] = [f"## {entry.title}", ""]
    reference = (
        f"`{entry.class_name}.{entry.method}`"
        if entry.class_name
        else f"`{entry.method}`"
    )

    deprecated_note = paragraph_text(str(generated.get("deprecated_note") or ""))
    if entry.is_deprecated:
        lines.extend([deprecated_note or "This zman is marked deprecated.", ""])

    meaning = paragraph_text(str(generated.get("meaning") or ""))
    calculation = paragraph_text(str(generated.get("calculation") or ""))
    raw_notes = generated.get("notes")
    note_values: list[Any] = raw_notes if isinstance(raw_notes, list) else []
    notes = [
        note
        for note in (paragraph_text(str(note)) for note in note_values)
        if note and not should_hide_note(entry, note)
    ]

    if meaning:
        lines.extend([meaning, ""])
    if calculation:
        lines.extend([calculation, ""])
    for note in notes:
        lines.extend([note, ""])

    lines.append('??? info "Technical details"')
    lines.append(f"    Source method: {reference}")
    lines.append("")
    lines.append(f"    Technical reference: `{entry.qualified_name}`")
    lines.append("")
    return "\n".join(lines)


def render_category_pages(
    groups: dict[str, list[SourceEntry]],
    generated_by_id: dict[str, dict[str, Any]],
    page_paths_by_category: dict[str, Path],
    index_filename: str,
) -> dict[Path, str]:
    pages: dict[Path, str] = {}
    for category, entries in groups.items():
        rendered_entries = [entry for entry in entries if entry.id in generated_by_id]
        if not rendered_entries:
            continue

        lines = [
            GENERATED_MARKER,
            "",
            f"# {category}",
            "",
            f"[Back to all zmanim]({index_filename})",
            "",
        ]
        for entry in rendered_entries:
            lines.append(render_item(entry, generated_by_id[entry.id]))
        pages[page_paths_by_category[category]] = "\n".join(lines).rstrip() + "\n"
    return pages


def load_generated_content(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_items: Any = data.get("items") if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise ValueError(f"Expected an items list in {path}")

    generated_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            generated_by_id[item["id"]] = item
    return generated_by_id


def write_markdown(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(markdown, encoding="utf-8")
    temp_path.replace(path)


def cleanup_generated_pages(pages_dir: Path) -> None:
    if not pages_dir.exists():
        return

    for path in pages_dir.glob("*.md"):
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
        except IndexError, OSError, UnicodeDecodeError:
            continue
        if first_line == GENERATED_MARKER:
            path.unlink()


def write_pages(pages: dict[Path, str]) -> None:
    for path, markdown in pages.items():
        write_markdown(path, markdown)


def main() -> int:
    args = parse_args()
    validate_category_config()
    items, encoding = load_json(args.input)
    entries = source_entries(items)
    groups = grouped_entries(entries)
    report = base_report(
        "render-md",
        args.input,
        str(args.generated),
        str(args.pages_dir),
        args.index_link,
        None,
        None,
        None,
        bool(args.dry_run),
        encoding,
        len(items),
        entries,
        groups,
    )
    add_missing_raw_skips(report, entries)

    try:
        generated_by_id = load_generated_content(args.generated)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report["warnings"].append(
            f"Could not read generated content from {args.generated}: {exc}"
        )
        write_json(args.report, report)
        print(report["warnings"][-1])
        return 1

    source_ids = {entry.id for entry in entries}
    for item_id in sorted(set(generated_by_id) - source_ids):
        report["warnings"].append(
            f"Generated content contains unknown item id not present in {args.input}: {item_id}"
        )
    generated_by_id = {
        item_id: item
        for item_id, item in generated_by_id.items()
        if item_id in source_ids
    }

    for entry in entries:
        if entry.raw_javadoc and entry.id not in generated_by_id:
            report["skipped_items"].append(
                {
                    "id": entry.id,
                    "title": entry.title,
                    "reason": "No generated content was found.",
                }
            )
    report["generated_items"] = len(generated_by_id)

    if args.dry_run:
        write_json(args.report, report)
        print_category_summary(items, entries, groups)
        print(
            f"Loaded {len(generated_by_id)} generated content items from {args.generated}."
        )
        print(f"Dry run report written to {args.report}")
        return 0

    if not generated_by_id:
        print("No usable generated content was found; category pages were not written.")
        write_json(args.report, report)
        return 1

    page_paths_by_category = category_page_paths(
        groups,
        generated_by_id,
        args.pages_dir,
    )
    pages = render_category_pages(
        groups,
        generated_by_id,
        page_paths_by_category,
        args.index_link,
    )

    cleanup_generated_pages(OLD_GENERATED_PAGES_DIR)
    cleanup_generated_pages(args.pages_dir)
    write_pages(pages)

    report["generated_pages"] = len(pages)
    write_json(args.report, report)
    print(f"Wrote {len(pages)} generated category pages.")
    print(f"Wrote generation report to {args.report}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
