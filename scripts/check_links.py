#!/usr/bin/env python3
"""Validate tracked course links and Colab notebook targets."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
REPO = "SECQUOIA/PU_CHE597_DSinChemE"
COLAB_BLOB_PREFIX = f"https://colab.research.google.com/github/{REPO}/blob/main/"
GITHUB_PATH_RE = re.compile(
    rf"https://github\.com/{re.escape(REPO)}/(?:blob|tree)/main/([^\"'<>\s)]+)"
)
COLAB_PATH_RE = re.compile(
    rf"{re.escape(COLAB_BLOB_PREFIX)}([^\"'<>\s)]+)"
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
HTML_LINK_RE = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']")
COLAB_MAP_RE = re.compile(r'"([^"]+)":\s*"([^"]+\.ipynb)"')
MYST_FILE_RE = re.compile(r"^\s*-?\s*file:\s*([^#\n]+?)\s*$")
TEXT_SUFFIXES = {".html", ".ipynb", ".md", ".yaml", ".yml"}
PATH_ASSIGN_RE = re.compile(r"\b(\w+)\s*=\s*Path\(\s*['\"]([^'\"]+)['\"]\s*\)")
READ_LOCAL_FILE_PATTERNS = (
    re.compile(r"\bpd\.read_(?:csv|excel|table|fwf)\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bpd\.read_(?:csv|excel|table|fwf)\(\s*(\w+)\s*(?:,|\))"),
    re.compile(r"\bnp\.(?:loadtxt|genfromtxt)\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bnp\.(?:loadtxt|genfromtxt)\(\s*(\w+)\s*(?:,|\))"),
    re.compile(r"\b(?:with\s+)?open\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*['\"]([^'\"]*)['\"])?"),
)
DOWNLOAD_MARKERS = ("!wget", "urlretrieve", "urlopen", "requests.get")


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    return [ROOT / line for line in output.splitlines()]


def read_source_text(path: Path) -> str:
    if path.suffix == ".ipynb":
        notebook = json.loads(path.read_text(encoding="utf-8"))
        sources: list[str] = []
        for cell in notebook.get("cells", []):
            source = cell.get("source", "")
            sources.append("".join(source) if isinstance(source, list) else source)
        return "\n".join(sources)

    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def repo_relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def split_link_target(link: str) -> str:
    return unquote(urlsplit(link.strip().strip("<>")).path)


def is_local_link(link: str) -> bool:
    link = link.strip()
    if not link or link.startswith("#"):
        return False
    parsed = urlsplit(link)
    return not parsed.scheme and not parsed.netloc


def check_local_links(files: list[Path], problems: list[str]) -> None:
    for path in files:
        if path.suffix not in TEXT_SUFFIXES:
            continue

        text = read_source_text(path)
        links = [match.group(1) for match in MARKDOWN_LINK_RE.finditer(text)]
        links.extend(match.group(1) for match in HTML_LINK_RE.finditer(text))

        for link in links:
            if not is_local_link(link):
                continue

            target_path = split_link_target(link)
            if not target_path:
                continue

            if target_path.startswith("/"):
                candidate = (ROOT / target_path.lstrip("/")).resolve()
            else:
                candidate = (path.parent / target_path).resolve()

            try:
                candidate.relative_to(ROOT)
            except ValueError:
                problems.append(f"{repo_relative(path)} links outside repo: {link}")
                continue

            if not candidate.exists():
                problems.append(f"{repo_relative(path)} has missing local link: {link}")


def check_colab_and_github_paths(files: list[Path], problems: list[str]) -> None:
    notebooks = {repo_relative(path) for path in files if path.suffix == ".ipynb"}
    notebooks_with_self_badge: set[str] = set()

    for path in files:
        if path.suffix not in TEXT_SUFFIXES:
            continue

        text = read_source_text(path)
        for pattern, label in (
            (COLAB_PATH_RE, "Colab"),
            (GITHUB_PATH_RE, "GitHub"),
        ):
            for match in pattern.finditer(text):
                target = split_link_target(match.group(1))
                if not (ROOT / target).exists():
                    problems.append(f"{repo_relative(path)} has missing {label} target: {target}")
                    continue

                if label == "Colab" and path.suffix == ".ipynb":
                    source_path = repo_relative(path)
                    if target == source_path:
                        notebooks_with_self_badge.add(source_path)
                    else:
                        problems.append(
                            f"{source_path} Colab badge points to {target}"
                        )

    missing_badges = sorted(notebooks - notebooks_with_self_badge)
    for notebook in missing_badges:
        problems.append(f"{notebook} is missing a self-targeting Colab badge")


def check_myst_toc(problems: list[str]) -> list[str]:
    myst_path = ROOT / "myst.yml"
    notebook_entries: list[str] = []
    for line in myst_path.read_text(encoding="utf-8").splitlines():
        match = MYST_FILE_RE.match(line)
        if not match:
            continue
        target = match.group(1).strip().strip("'\"")
        if target.endswith(".ipynb"):
            notebook_entries.append(target)
        if not (ROOT / target).exists():
            problems.append(f"myst.yml references missing file: {target}")
    return notebook_entries


def check_colab_redirect(notebook_entries: list[str], problems: list[str]) -> None:
    colab_path = ROOT / "colab.html"
    html = colab_path.read_text(encoding="utf-8")
    entries = dict(COLAB_MAP_RE.findall(html))

    for key, target in sorted(entries.items()):
        if not (ROOT / target).exists():
            problems.append(f"colab.html maps {key} to missing notebook: {target}")

    mapped_targets = set(entries.values())
    for notebook in sorted(notebook_entries):
        if notebook not in mapped_targets:
            problems.append(f"colab.html has no generated-page alias for {notebook}")

    expected_fallback_tokens = (
        "const notebookPaths = new Set(Object.values(map));",
        "notebookPaths.has(directNotebookPath)",
    )
    for token in expected_fallback_tokens:
        if token not in html:
            problems.append("colab.html is missing source-path redirect fallback")
            break


def check_notebook_runtime_files(files: list[Path], problems: list[str]) -> None:
    tracked = {repo_relative(path) for path in files}

    for path in files:
        if path.suffix != ".ipynb":
            continue

        notebook = json.loads(path.read_text(encoding="utf-8"))
        path_vars: dict[str, str] = {}
        previous_source = ""

        for cell in notebook.get("cells", []):
            source = cell.get("source", "")
            source_text = "".join(source) if isinstance(source, list) else source

            for var_name, filename in PATH_ASSIGN_RE.findall(source_text):
                path_vars[var_name] = filename

            if cell.get("cell_type") == "code":
                for pattern in READ_LOCAL_FILE_PATTERNS:
                    for match in pattern.finditer(source_text):
                        target_token = match.group(1)
                        filename = path_vars.get(target_token, target_token)
                        mode = match.group(2) if len(match.groups()) > 1 else ""

                        if mode and any(flag in mode for flag in ("w", "a", "x", "+")):
                            continue

                        parsed = urlsplit(filename)
                        if (
                            parsed.scheme
                            or parsed.netloc
                            or not filename
                            or filename.startswith("/")
                            or "/" in filename
                        ):
                            continue

                        candidate = path.parent / filename
                        if repo_relative(candidate) not in tracked:
                            continue

                        source_before_read = previous_source + source_text[: match.start()]
                        has_download_guard = filename in source_before_read and any(
                            marker in source_before_read for marker in DOWNLOAD_MARKERS
                        )
                        if not has_download_guard:
                            problems.append(
                                f"{repo_relative(path)} reads {filename} before a Colab download guard"
                            )

            previous_source += "\n" + source_text


def main() -> int:
    files = tracked_files()
    problems: list[str] = []

    check_local_links(files, problems)
    check_colab_and_github_paths(files, problems)
    notebook_entries = check_myst_toc(problems)
    check_colab_redirect(notebook_entries, problems)
    check_notebook_runtime_files(files, problems)

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print("All tracked local, GitHub, and Colab links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
