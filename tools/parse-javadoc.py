from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any

import tree_sitter_java
from tree_sitter import Language, Node, Parser


DEFAULT_SOURCE = Path("kosher-java/src/main/java")

JAVA_LANGUAGE = Language(tree_sitter_java.language())
JAVA_PARSER = Parser()
JAVA_PARSER.language = JAVA_LANGUAGE

TAG_RE = re.compile(r"^@(\w+)\s*(.*)")
INLINE_TAG_RE = re.compile(r"\{@(\w+)\s+([^{}]*)\}")
HTML_TAG_RE = re.compile(r"</?[^>]+>")


def strip_java_noise(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("/**"):
        stripped = stripped[3:]
    if stripped.endswith("*/"):
        stripped = stripped[:-2]
    if stripped.startswith("*"):
        stripped = stripped[1:]
        if stripped.startswith(" "):
            stripped = stripped[1:]
    return stripped.rstrip()


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def inline_tag_text(match: re.Match[str]) -> str:
    tag_name = match.group(1)
    content = normalize_spaces(match.group(2))
    if tag_name in {"code", "literal"}:
        return content

    # For links, prefer the user-facing label when one is present.
    parts = content.split()
    if tag_name in {"link", "linkplain", "value"} and len(parts) > 1:
        return " ".join(parts[1:])
    return content


def clean_doc_text(text: str) -> str:
    text = INLINE_TAG_RE.sub(inline_tag_text, text)
    text = HTML_TAG_RE.sub("", text)
    return normalize_spaces(html.unescape(text))


def first_sentence(text: str) -> str:
    if not text:
        return ""
    match = re.search(r"(?<=[.!?])\s+", text)
    if match is None:
        return text
    return text[: match.start()].strip()


def unwrap_summary(text: str) -> tuple[str | None, str]:
    start = text.find("{@summary")
    if start == -1:
        return None, text

    content_start = start + len("{@summary")
    index = content_start
    depth = 1
    while index < len(text):
        if text.startswith("{@", index):
            depth += 1
            index += 2
            continue
        if text[index] == "}":
            depth -= 1
            if depth == 0:
                summary = text[content_start:index].strip()
                unwrapped = f"{text[:start]}{summary}{text[index + 1 :]}"
                return summary, unwrapped
        index += 1

    return None, text


def relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def node_text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return source[node.start_byte : node.end_byte].decode("utf-8")


def walk(node: Node) -> list[Node]:
    nodes = [node]
    for child in node.children:
        nodes.extend(walk(child))
    return nodes


def find_child(node: Node, node_type: str) -> Node | None:
    return next((child for child in node.children if child.type == node_type), None)


def package_name(root: Node, source: bytes) -> str:
    package = find_child(root, "package_declaration")
    if package is None:
        return ""

    for child in package.children:
        if child.type in {"identifier", "scoped_identifier"}:
            return node_text(child, source)
    return ""


def enclosing_class(node: Node, source: bytes) -> str:
    class_names: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in {
            "class_declaration",
            "interface_declaration",
            "record_declaration",
            "enum_declaration",
        }:
            name = node_text(current.child_by_field_name("name"), source)
            if name:
                class_names.append(name)
        current = current.parent
    return ".".join(reversed(class_names))


def method_modifiers(method: Node) -> Node | None:
    return find_child(method, "modifiers")


def has_public_modifier(method: Node) -> bool:
    modifiers = method_modifiers(method)
    return modifiers is not None and any(
        child.type == "public" for child in modifiers.children
    )


def method_annotations(method: Node, source: bytes) -> list[str]:
    modifiers = method_modifiers(method)
    if modifiers is None:
        return []
    return [
        node_text(child, source)
        for child in modifiers.children
        if "annotation" in child.type
    ]


def public_line(method: Node) -> int:
    modifiers = method_modifiers(method)
    if modifiers is not None:
        public = next(
            (child for child in modifiers.children if child.type == "public"), None
        )
        if public is not None:
            return public.start_point[0] + 1
    return method.start_point[0] + 1


def is_no_arg_method(method: Node) -> bool:
    parameters = method.child_by_field_name("parameters")
    if parameters is None:
        return False
    return not any("parameter" in child.type for child in parameters.children)


def returns_instant(method: Node, source: bytes) -> bool:
    return_type = method.child_by_field_name("type")
    return node_text(return_type, source) in {"Instant", "java.time.Instant"}


def preceding_javadoc(method: Node, source: bytes) -> list[str]:
    sibling = method.prev_named_sibling
    if sibling is None or sibling.type != "block_comment":
        return []

    comment = node_text(sibling, source)
    if not comment.lstrip().startswith("/**"):
        return []
    return comment.splitlines()


def parse_javadoc(lines: list[str]) -> dict[str, Any]:
    cleaned_lines = [strip_java_noise(line) for line in lines]
    raw_text = "\n".join(cleaned_lines).strip()

    main_lines: list[str] = []
    tags: dict[str, list[str]] = {}
    current_tag: str | None = None

    for line in cleaned_lines:
        tag_match = TAG_RE.match(line)
        if tag_match:
            current_tag = tag_match.group(1)
            tags.setdefault(current_tag, []).append(tag_match.group(2).strip())
        elif current_tag is not None and line:
            tags[current_tag][-1] = f"{tags[current_tag][-1]} {line}".strip()
        elif current_tag is None:
            main_lines.append(line)
        elif not line:
            current_tag = None

    main_text = "\n".join(main_lines).strip()
    summary_text, main_text = unwrap_summary(main_text)
    description = clean_doc_text(main_text)
    summary = (
        clean_doc_text(summary_text) if summary_text else first_sentence(description)
    )

    parsed_tags = {
        tag: [clean_doc_text(value) for value in values if clean_doc_text(value)]
        for tag, values in tags.items()
    }

    return {
        "summary": summary,
        "description": description,
        "return": (parsed_tags.get("return") or [""])[0],
        "deprecated": (parsed_tags.get("deprecated") or [""])[0],
        "see": parsed_tags.get("see", []),
        "tags": parsed_tags,
        "raw": clean_doc_text(raw_text),
    }


def parse_java_file(path: Path) -> list[dict[str, Any]]:
    source = path.read_bytes()
    tree = JAVA_PARSER.parse(source)
    root = tree.root_node
    package = package_name(root, source)
    methods: list[dict[str, Any]] = []

    for method in walk(root):
        if method.type != "method_declaration":
            continue
        if not has_public_modifier(method) or not returns_instant(method, source):
            continue
        if not is_no_arg_method(method):
            continue

        class_name = enclosing_class(method, source)
        qualified_class = ".".join(part for part in (package, class_name) if part)
        method_name = node_text(method.child_by_field_name("name"), source)
        annotations = method_annotations(method, source)
        javadocs = preceding_javadoc(method, source)
        docs = parse_javadoc(javadocs) if javadocs else empty_docs()

        methods.append(
            {
                "name": method_name,
                "qualified_name": ".".join(
                    part for part in (qualified_class, method_name) if part
                ),
                "package": package,
                "class": class_name,
                "qualified_class": qualified_class,
                "file": relative_path(path),
                "line": public_line(method),
                "return_type": node_text(method.child_by_field_name("type"), source),
                "parameters": [],
                "annotations": annotations,
                "is_deprecated": bool(docs["deprecated"])
                or any(
                    annotation.startswith("@Deprecated") for annotation in annotations
                ),
                "docs": docs,
            }
        )

    return methods


def empty_docs() -> dict[str, Any]:
    return {
        "summary": "",
        "description": "",
        "return": "",
        "deprecated": "",
        "see": [],
        "tags": {},
        "raw": "",
    }


def collect_methods(source: Path) -> list[dict[str, Any]]:
    source = source.resolve()
    methods: list[dict[str, Any]] = []
    for java_file in sorted(source.rglob("*.java")):
        methods.extend(parse_java_file(java_file))
    return methods


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract public no-argument Java methods that return Instant.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Java source tree to scan (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    methods = collect_methods(args.source)
    indent = 2 if args.pretty else None
    with open("methods.json", "w", encoding="utf-8") as f:
        json.dump(methods, f, indent=indent)


if __name__ == "__main__":
    main()
