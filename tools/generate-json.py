from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from docs_common import (
    DEFAULT_ENV_FILE,
    DEFAULT_GENERATED_OUTPUT,
    DEFAULT_INPUT,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_REPORT,
    DEFAULT_SEED,
    ReasoningEffort,
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
from openai import AsyncOpenAI


@dataclass(frozen=True)
class BatchJob:
    index: int
    category: str
    entries: list[SourceEntry]


@dataclass(frozen=True)
class BatchResult:
    job: BatchJob
    output: dict[str, dict[str, Any]]
    warnings: list[str]
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call GPT and write generated zmanim documentation JSON.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Parsed methods JSON to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--generated-output",
        type=Path,
        default=DEFAULT_GENERATED_OUTPUT,
        help=f"Generated content JSON to write (default: {DEFAULT_GENERATED_OUTPUT})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Generation report JSON to write (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Optional env file with OPENAI-KEY or OPENAI_API_KEY (default: {DEFAULT_ENV_FILE})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
        help=f"OpenAI model to use (default: OPENAI_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Number of zmanim to send to the model in each request.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=120.0,
        help="OpenAI request timeout in seconds.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Seed to reuse for model generation (default: {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh"),
        default=DEFAULT_REASONING_EFFORT,
        help=f"Reasoning effort for GPT models that support it (default: {DEFAULT_REASONING_EFFORT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input, grouping, and prompt construction without calling OpenAI.",
    )
    return parser.parse_args()


def load_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def openai_api_key(env_file: Path) -> str | None:
    env_values = load_env_values(env_file)
    return (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENAI-KEY")
        or env_values.get("OPENAI_API_KEY")
        or env_values.get("OPENAI-KEY")
    )


def batched(entries: list[SourceEntry], batch_size: int) -> list[list[SourceEntry]]:
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    return [
        entries[index : index + batch_size]
        for index in range(0, len(entries), batch_size)
    ]


def model_payload(entry: SourceEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "title": entry.title,
        "method": entry.method,
        "class": entry.class_name,
        "is_deprecated": entry.is_deprecated,
        "raw_javadoc": entry.raw_javadoc,
    }


def system_prompt() -> str:
    return (
        "You turn raw Java Javadoc for KosherJava zmanim methods into concise, "
        "user-facing documentation for people trying to understand zmanim, not "
        "developers trying to use an API. You must be evidence-bound: use only facts "
        "directly supported by each item's raw_javadoc. Do not add halachic, "
        "astronomical, historical, API, or usage context unless it appears in "
        "raw_javadoc. Never mention Javadoc, source code, methods, APIs, return "
        "values, or Java concepts in user-facing fields. Translate developer terms "
        "into plain language: for example, do not say 'returns null'; say that the "
        "time may not be available or cannot be calculated in that situation. If "
        "raw_javadoc includes Javadoc inline links and HTML links; preserve useful "
        "user-facing links as Markdown when you use the linked text, but do not add "
        "links that are not present in the raw_javadoc. If the raw_javadoc does not "
        "support a field, return an empty string or an empty list for that field. "
        "Assume all astronomical calculations use the NOAA calculator. If raw_javadoc "
        "discusses behavior that depends on which astronomical calculator is used, "
        "ignore the alternate-calculator behavior and explain only the NOAA behavior."
    )


def user_prompt(category: str, entries: list[SourceEntry]) -> str:
    payload = {
        "category": category,
        "requirements": [
            "Return valid JSON only.",
            "Return one output item for every input id.",
            "Do not include markdown headings in fields.",
            "Write fields as plain paragraph text, without labels, bullets, bold text, or other markdown formatting.",
            "Write for end users only. Do not mention Javadoc, API, Java, methods, return values, source, or null.",
            "If raw_javadoc says null will be returned, explain that the zman may not be available or cannot be calculated in that case.",
            "Preserve useful Javadoc or HTML links from raw_javadoc as Markdown when using the linked text. Do not create new links.",
            "Keep meaning and calculation concise.",
            "Assume the NOAA calculator is used. Do not describe alternate astronomical calculator behavior.",
            "Mention deprecated status only when is_deprecated is true or raw_javadoc explains deprecation.",
            "For numeric settings or offsets, say the time depends on that setting only when raw_javadoc supports it.",
            "For Kidush Levana, do not say that the zman may not be available when it does not occur on this day.",
        ],
        "output_schema": {
            "items": [
                {
                    "id": "same id as input",
                    "meaning": "plain-English meaning, source-backed",
                    "calculation": "how it is calculated, source-backed or empty",
                    "notes": ["source-backed caveats, null behavior, or settings"],
                    "deprecated_note": "source-backed warning if deprecated, otherwise empty",
                }
            ]
        },
        "items": [model_payload(entry) for entry in entries],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def call_model(
    client: AsyncOpenAI,
    model: str,
    seed: int,
    reasoning_effort: ReasoningEffort,
    category: str,
    entries: list[SourceEntry],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    completion = await client.chat.completions.create(
        model=model,
        seed=seed,
        reasoning_effort=reasoning_effort,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user_prompt(category, entries)},
        ],
    )
    content = completion.choices[0].message.content or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return {}, [f"Model returned invalid JSON for {category}: {exc}"]

    output_items = parsed.get("items")
    if not isinstance(output_items, list):
        return {}, [f"Model response for {category} did not contain an items list."]

    by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    expected_ids = {entry.id for entry in entries}

    for output_item in output_items:
        if not isinstance(output_item, dict):
            warnings.append(f"Model returned a non-object item in {category}.")
            continue
        item_id = output_item.get("id")
        if not isinstance(item_id, str) or item_id not in expected_ids:
            warnings.append(
                f"Model returned an unknown item id in {category}: {item_id!r}"
            )
            continue
        by_id[item_id] = output_item

    for missing_id in sorted(expected_ids - set(by_id)):
        warnings.append(f"Model omitted {missing_id} in {category}.")

    return by_id, warnings


async def run_batch_job(
    client: AsyncOpenAI,
    model: str,
    seed: int,
    reasoning_effort: ReasoningEffort,
    job: BatchJob,
) -> BatchResult:
    try:
        output, warnings = await call_model(
            client, model, seed, reasoning_effort, job.category, job.entries
        )
    except Exception as exc:
        return BatchResult(job=job, output={}, warnings=[], error=str(exc))
    return BatchResult(job=job, output=output, warnings=warnings)


async def generate_model_outputs(
    api_key: str,
    model: str,
    seed: int,
    reasoning_effort: ReasoningEffort,
    request_timeout: float,
    batch_jobs: list[BatchJob],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    generated_by_id: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    if not batch_jobs:
        return generated_by_id, warnings

    client = AsyncOpenAI(api_key=api_key, timeout=request_timeout, max_retries=1)
    tasks = [
        asyncio.create_task(run_batch_job(client, model, seed, reasoning_effort, job))
        for job in batch_jobs
    ]

    try:
        for completed_count, completed_task in enumerate(
            asyncio.as_completed(tasks), start=1
        ):
            result = await completed_task
            print(
                f"Finished batch {completed_count}/{len(batch_jobs)}: {result.job.category} ({result.job.index})",
                flush=True,
            )
            if result.error:
                warnings.append(
                    f"OpenAI request failed for {result.job.category} batch {result.job.index}: {result.error}"
                )
                continue
            generated_by_id.update(result.output)
            warnings.extend(result.warnings)
    finally:
        await client.close()

    return generated_by_id, warnings


def generated_content_json(
    args: argparse.Namespace,
    encoding: str,
    source_count: int,
    entries: list[SourceEntry],
    groups: dict[str, list[SourceEntry]],
    generated_by_id: dict[str, dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "input": str(args.input),
        "model": args.model,
        "seed": args.seed,
        "reasoning_effort": args.reasoning_effort,
        "source_encoding": encoding,
        "source_entries": source_count,
        "render_entries": len(entries),
        "categories": {
            category: len(category_entries)
            for category, category_entries in groups.items()
        },
        "items": [generated_by_id[item_id] for item_id in sorted(generated_by_id)],
        "skipped_items": report["skipped_items"],
        "warnings": report["warnings"],
    }


def main() -> int:
    args = parse_args()
    validate_category_config()
    items, encoding = load_json(args.input)
    entries = source_entries(items)
    groups = grouped_entries(entries)
    report = base_report(
        "generate-json",
        args.input,
        str(args.generated_output),
        "",
        "",
        args.model,
        args.seed,
        args.reasoning_effort,
        bool(args.dry_run),
        encoding,
        len(items),
        entries,
        groups,
    )
    add_missing_raw_skips(report, entries)

    eligible_groups = {
        category: [entry for entry in category_entries if entry.raw_javadoc]
        for category, category_entries in groups.items()
    }

    if args.dry_run:
        write_json(args.report, report)
        print_category_summary(items, entries, groups)
        print(f"Dry run report written to {args.report}")
        return 0

    api_key = openai_api_key(args.env_file)
    if not api_key:
        report["warnings"].append(
            f"No OpenAI API key found. Set OPENAI_API_KEY or add OPENAI-KEY to {args.env_file}."
        )
        write_json(args.report, report)
        print(report["warnings"][-1], file=sys.stderr)
        return 1

    batch_jobs: list[BatchJob] = []
    for category, category_entries in eligible_groups.items():
        for batch in batched(category_entries, args.batch_size):
            batch_jobs.append(BatchJob(len(batch_jobs) + 1, category, batch))

    print(f"Generating {len(batch_jobs)} batches asynchronously...", flush=True)
    generated_by_id, generation_warnings = asyncio.run(
        generate_model_outputs(
            api_key,
            args.model,
            args.seed,
            cast(ReasoningEffort, args.reasoning_effort),
            args.request_timeout,
            batch_jobs,
        )
    )
    report["warnings"].extend(generation_warnings)

    for entry in entries:
        if entry.raw_javadoc and entry.id not in generated_by_id:
            report["skipped_items"].append(
                {
                    "id": entry.id,
                    "title": entry.title,
                    "reason": "No usable model output was returned.",
                }
            )

    report["generated_items"] = len(generated_by_id)

    if not generated_by_id:
        print(
            f"No usable content was generated; {args.generated_output} was not written.",
            file=sys.stderr,
        )
        write_json(args.report, report)
        return 1

    write_json(
        args.generated_output,
        generated_content_json(
            args,
            encoding,
            len(items),
            entries,
            groups,
            generated_by_id,
            report,
        ),
    )
    write_json(args.report, report)
    print(f"Wrote generated content to {args.generated_output}.")
    print(f"Wrote generation report to {args.report}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
