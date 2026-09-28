"""Harness-aligned str_replace_editor over WorkspaceSandbox."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .sandbox import SandboxError, WorkspaceSandbox

TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>To save on context only part of this file has been shown to you. "
    "You should retry this tool after you have searched inside the file with `grep -n` in order "
    "to find the line numbers of what you are looking for.</NOTE>"
)

DEFAULT_DESCRIPTION = """
Custom editing tool for viewing, creating and editing files
* State is persistent across command calls and discussions with the user
* If `path` is a file, `view` displays the result of applying `cat -n`. If `path` is a directory, `view` lists non-hidden files and directories up to 2 levels deep
* The `create` command cannot be used if the specified `path` already exists as a file
* If a `command` generates a long output, it will be truncated and marked with `<response clipped>`

Notes for using the `str_replace` command:
* The `old_str` parameter should match EXACTLY one or more consecutive lines from the original file. Be mindful of whitespaces!
* If the `old_str` parameter is not unique in the file, the replacement will not be performed. Make sure to include enough context in `old_str` to make it unique
* The `new_str` parameter should contain the edited lines that should replace the `old_str`
""".strip()


class EditorError(ValueError):
    """User-facing editor failure (ambiguous edit, missing file, etc.)."""


def maybe_truncate(content: str, max_output_chars: int) -> str:
    if len(content) <= max_output_chars:
        return content
    return content[:max_output_chars] + TRUNCATED_MESSAGE


def _match_offsets(content: str, search: str) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        idx = content.find(search, start)
        if idx < 0:
            return offsets
        offsets.append(idx)
        start = idx + len(search)


def _line_numbers_at(content: str, offsets: list[int]) -> list[int]:
    line = 1
    cursor = 0
    out: list[int] = []
    for offset in offsets:
        while cursor < offset:
            if content[cursor] == "\n":
                line += 1
            cursor += 1
        out.append(line)
    return out


def _format_file_view(
    path: str, content: str, max_output_chars: int, view_range: list[int] | None
) -> str:
    all_lines = content.split("\n")
    lines = all_lines
    initial_line = 1
    prompt = (
        f"Here's the content of {path} with line numbers "
        f"(which has a total of {len(all_lines)} lines)"
    )
    if view_range is not None:
        if len(view_range) != 2 or not all(isinstance(x, int) for x in view_range):
            raise EditorError("Invalid `view_range`. It should be a list of two integers.")
        initial_line, final_line = view_range
        if initial_line < 1 or initial_line > len(all_lines):
            raise EditorError(
                f"Invalid `view_range`: [{view_range[0]}, {view_range[1]}]. "
                f"Its first element `{initial_line}` should be within the range of lines "
                f"of the file: [1, {len(all_lines)}]"
            )
        if final_line > len(all_lines):
            raise EditorError(
                f"Invalid `view_range`: [{view_range[0]}, {view_range[1]}]. "
                f"Its second element `{final_line}` should be smaller than the number of "
                f"lines in the file: `{len(all_lines)}`"
            )
        if final_line != -1 and final_line < initial_line:
            raise EditorError(
                f"Invalid `view_range`: [{view_range[0]}, {view_range[1]}]. "
                f"Its second element `{final_line}` should be larger or equal than its first "
                f"`{initial_line}`"
            )
        lines = (
            all_lines[initial_line - 1 :]
            if final_line == -1
            else all_lines[initial_line - 1 : final_line]
        )
        prompt += f" with view_range=[{initial_line}, {final_line}]"
    numbered = "\n".join(
        f"{str(initial_line + i).rjust(6)}  {line}" for i, line in enumerate(lines)
    )
    return maybe_truncate(f"{prompt}:\n{numbered}\n", max_output_chars)


def _list_directory_sandbox(
    sandbox: WorkspaceSandbox, target: Path, max_output_chars: int
) -> str:
    display = sandbox.relpath(target) if target != sandbox.root else "."
    rows: list[str] = [f"d\t{display}"]

    def visit(dir_path: Path, depth: int) -> None:
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__"}:
                continue
            shown = sandbox.relpath(entry)
            kind = "d" if entry.is_dir() else "f" if entry.is_file() else "?"
            rows.append(f"{kind}\t{shown}")
            if entry.is_dir() and depth < 2:
                visit(entry, depth + 1)

    visit(target, 1)
    listing = maybe_truncate("\n".join(rows) + "\n", max_output_chars)
    return (
        f"Here're the files and directories up to 2 levels deep in {display}, "
        f"excluding hidden items, node_modules, and Python cache directories:\n{listing}\n"
    )


async def run_str_replace_editor(
    sandbox: WorkspaceSandbox,
    *,
    command: Literal["view", "create", "str_replace", "insert"],
    path: str,
    file_text: str | None = None,
    old_str: str | None = None,
    new_str: str | None = None,
    insert_line: int | None = None,
    view_range: list[int] | None = None,
    max_output_chars: int = 16000,
) -> str:
    return await asyncio.to_thread(
        _run_sync,
        sandbox,
        command,
        path,
        file_text,
        old_str,
        new_str,
        insert_line,
        view_range,
        max_output_chars,
    )


def _run_sync(
    sandbox: WorkspaceSandbox,
    command: str,
    path: str,
    file_text: str | None,
    old_str: str | None,
    new_str: str | None,
    insert_line: int | None,
    view_range: list[int] | None,
    max_output_chars: int,
) -> str:
    target = sandbox.resolve(path)
    display = sandbox.relpath(target)

    if command == "view":
        if not target.exists():
            raise EditorError(f"The path {display} does not exist. Please provide a valid path.")
        if target.is_dir():
            if view_range is not None:
                raise EditorError(
                    "The `view_range` parameter is not allowed when `path` points to a directory."
                )
            return _list_directory_sandbox(sandbox, target, max_output_chars)
        if not target.is_file():
            raise EditorError(f'cannot view "{display}": not a regular file or directory')
        content = target.read_text(encoding="utf-8")
        return _format_file_view(display, content, max_output_chars, view_range)

    if command == "create":
        if file_text is None:
            raise EditorError("Parameter `file_text` is required for command: create")
        sandbox.assert_writable(target)
        if target.exists():
            raise EditorError(
                f"File already exists at: {display}. Cannot overwrite files using command `create`."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text, encoding="utf-8")
        return f"New file created successfully at: {display}"

    if command == "str_replace":
        if old_str is None or old_str == "":
            raise EditorError("Parameter `old_str` is required for command: str_replace")
        if new_str is None:
            new_str = ""
        sandbox.assert_writable(target)
        if not target.exists():
            raise EditorError(f"The path {display} does not exist. Please provide a valid path.")
        if target.is_dir():
            raise EditorError(
                f"The path {display} is a directory and only the `view` command can be used on directories"
            )
        before = target.read_text(encoding="utf-8")
        offsets = _match_offsets(before, old_str)
        if not offsets:
            raise EditorError(
                f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {display}."
            )
        if len(offsets) > 1:
            lines = _line_numbers_at(before, offsets)
            raise EditorError(
                f"No replacement was performed. Multiple occurrences of old_str `{old_str}` "
                f"in lines [{', '.join(map(str, lines))}]. Please ensure it is unique"
            )
        after = before[: offsets[0]] + new_str + before[offsets[0] + len(old_str) :]
        target.write_text(after, encoding="utf-8")
        return f"The file {display} has been edited successfully."

    if command == "insert":
        if insert_line is None:
            raise EditorError("Parameter `insert_line` is required for command: insert")
        if new_str is None:
            raise EditorError("Parameter `new_str` is required for command: insert")
        sandbox.assert_writable(target)
        if not target.exists():
            raise EditorError(f"The path {display} does not exist. Please provide a valid path.")
        if not target.is_file():
            raise EditorError(f'cannot insert into "{display}": not a regular file')
        before = target.read_text(encoding="utf-8")
        lines = before.split("\n")
        if not isinstance(insert_line, int) or insert_line < 0 or insert_line > len(lines):
            raise EditorError(
                f"Invalid `insert_line` parameter: {insert_line}. It should be within the range "
                f"of lines of the file: [0, {len(lines)}]"
            )
        after_lines = [
            *lines[:insert_line],
            *new_str.split("\n"),
            *lines[insert_line:],
        ]
        target.write_text("\n".join(after_lines), encoding="utf-8")
        return f"The file {display} has been edited successfully."

    raise EditorError(
        f"Unknown command `{command}`. Allowed options are: `view`, `create`, `str_replace`, `insert`."
    )


class StrReplaceEditorArgs(BaseModel):
    command: Literal["view", "create", "str_replace", "insert"] = Field(
        description="The commands to run. Allowed options are: `view`, `create`, `str_replace`, `insert`."
    )
    path: str = Field(description="Path to file or directory relative to the workspace root.")
    file_text: str | None = Field(
        default=None,
        description="Required parameter of `create` command, with the content of the file to be created.",
    )
    insert_line: int | None = Field(
        default=None,
        description="Required parameter of `insert` command. The `new_str` will be inserted AFTER the line `insert_line` of `path`.",
    )
    new_str: str | None = Field(
        default=None,
        description="Optional parameter of `str_replace` command containing the new string. Required for `insert`.",
    )
    old_str: str | None = Field(
        default=None,
        description="Required parameter of `str_replace` command containing the string in `path` to replace.",
    )
    view_range: list[int] | None = Field(
        default=None,
        description="Optional parameter of `view` when path is a file, e.g. [11, 12] or [start, -1].",
    )


def make_str_replace_editor_tool(
    sandbox: WorkspaceSandbox, *, max_output_chars: int = 16000
) -> StructuredTool:
    async def _run(**kwargs: Any) -> str:
        try:
            return await run_str_replace_editor(
                sandbox, max_output_chars=max_output_chars, **kwargs
            )
        except (SandboxError, EditorError) as exc:
            return f"Error: {exc}"

    return StructuredTool.from_function(
        coroutine=_run,
        name="str_replace_editor",
        description=DEFAULT_DESCRIPTION,
        args_schema=StrReplaceEditorArgs,
    )
