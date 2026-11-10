"""Configuration loading. Values may reference environment variables as ${VAR}."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class Config:
    raw: dict
    path: Path

    # ---- helpers -------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node if node is not None else default

    @property
    def state_dir(self) -> Path:
        p = Path(os.path.expanduser(self.get("general.state_dir", "~/.local/share/castsync")))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def work_dir(self) -> Path:
        p = Path(os.path.expanduser(self.get("general.work_dir", str(self.state_dir / "work"))))
        p.mkdir(parents=True, exist_ok=True)
        return p


class ConfigError(Exception):
    """A config problem explained in plain language (no Python traceback)."""


def _explain_yaml_error(path: Path, err: yaml.YAMLError) -> str:
    mark = getattr(err, "problem_mark", None)
    problem = getattr(err, "problem", None) or str(err)
    if mark is None:
        return f"{path} isn't valid YAML: {problem}"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = mark.line  # 0-based
    msg = [f"There's a formatting mistake in {path}",
           f"on line {n + 1}, column {mark.column + 1}: {problem}", ""]
    for i in range(max(0, n - 2), min(len(lines), n + 2)):
        pointer = ">>" if i == n else "  "
        msg.append(f" {pointer} {i + 1:4}| {lines[i]}")
    msg.append("")
    hints = []
    bad = lines[n] if n < len(lines) else ""
    indent = len(bad) - len(bad.lstrip(" "))
    if "\t" in bad[: len(bad) - len(bad.lstrip())]:
        hints.append("This line starts with a TAB. YAML only allows spaces - replace the tab with spaces.")
    # indentation of the nearest non-comment lines above
    prev = [l for l in lines[:n] if l.strip() and not l.lstrip().startswith("#")]
    prev_indent = len(prev[-1]) - len(prev[-1].lstrip(" ")) if prev else 0
    prev_is_header = bool(prev) and prev[-1].split(" #")[0].rstrip().endswith(":")
    expected = {prev_indent + 2} if prev_is_header else {prev_indent}
    if not prev_is_header:
        expected |= set(range(0, prev_indent + 1, 2))  # closing a section is fine too
    if bad.strip() and indent not in expected and not hints:  # (tab hint already covers it)
        if indent % 2 == 1:
            hints.append(f"This line is indented {indent} spaces but should be "
                         f"{min(expected, key=lambda e: abs(e - indent))}. If you un-commented it, "
                         "delete the '#' AND the space after it, so it lines up with the lines around it.")
        else:
            hints.append(f"Check the indentation: this line has {indent} spaces; lines in the same "
                         "section must line up exactly with each other.")
    value = bad.split(":", 1)[1] if ":" in bad else ""
    if ": " in value.split(" #")[0].strip():
        hints.append("The value contains ': '. Put the whole value in quotes, e.g. "
                     'show_name: "My Show: Live"')
    if problem and "found character '%'" in problem or "'@'" in str(problem) or "'`'" in str(problem):
        hints.append("Values starting with @, % or ` must be in quotes.")
    if not hints:
        hints.append("Common causes: indentation that doesn't line up, a tab instead of spaces, "
                     "or a value containing ': ' that needs quotes.")
    msg += ["How to fix: " + h for h in hints]
    msg.append(f"\nOpen it with:  nano {path}   (Ctrl+_ then {n + 1} jumps to the line)")
    return "\n".join(msg)


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"No config file at {path}. Run ./install.sh, or copy "
                          "config.example.yaml there and edit it.")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as e:
        raise ConfigError(_explain_yaml_error(path, e)) from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should contain settings like 'general:' and 'email:', "
                          "but it doesn't look like that. Compare it with config.example.yaml.")
    # Optional .env file next to the config for secrets
    env_file = path.parent / "castsync.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return Config(raw=_expand(data), path=path)
