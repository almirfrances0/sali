"""Skill discovery — scan a directory of human-editable Markdown skills and parse their metadata.

A skill is a `.md` file with optional YAML-ish frontmatter (`name`, `tags`). Frontmatter is parsed
tolerantly WITHOUT a YAML dependency (only the two fields Sali needs), and a file with no frontmatter
still works — its name is the filename and tags are derived from the filename. The user can drop a new
`.md` file in and it is discovered with no code change (§6/§8).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sali.obs.log import get_logger

log = get_logger("sali.skills")

_FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass(slots=True)
class SkillFile:
    name: str                       # slug — the filename stem (stable identity)
    title: str                      # human title (frontmatter `name`, else the slug)
    path: str
    tags: list[str] = field(default_factory=list)
    content: str = ""               # the markdown body (frontmatter stripped)
    content_hash: str = ""          # sha256 of the raw file — the snapshot version key


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split `--- ... ---` frontmatter from the body. Returns ({name, tags}, body). Tolerant: a file
    with no frontmatter returns ({}, text). Only `name:` and `tags:` (either `[a, b]` or a `- item`
    list) are understood — enough for selection, without pulling in a YAML parser."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict[str, object] = {}
    tags: list[str] = []
    in_tags = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if in_tags and stripped.startswith("- "):
            tags.append(stripped[2:].strip().strip("\"'"))
            continue
        in_tags = False
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "name":
            meta["name"] = val.strip("\"'")
        elif key == "tags":
            if val.startswith("[") and val.endswith("]"):  # inline list: [a, b, c]
                tags = [t.strip().strip("\"'") for t in val[1:-1].split(",") if t.strip()]
            elif val:
                tags = [t.strip().strip("\"'") for t in val.split(",") if t.strip()]
            else:
                in_tags = True  # a block list follows on subsequent `- item` lines
    if tags:
        meta["tags"] = tags
    return meta, body.strip()


def _skill_from_file(path: Path) -> SkillFile | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("skill_read_failed", path=str(path), error=str(exc))
        return None
    meta, body = parse_frontmatter(raw)
    slug = path.stem.lower()
    raw_tags = meta.get("tags")
    tags = [str(t).lower() for t in raw_tags] if isinstance(raw_tags, list) else []
    if not tags:  # no frontmatter tags → derive from the filename so bare-Markdown skills still match
        tags = [w for w in re.split(r"[-_\s]+", slug) if w]
    return SkillFile(
        name=slug,
        title=str(meta.get("name") or slug),
        path=str(path),
        tags=tags,
        content=body or raw.strip(),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def discover_skills(root: Path) -> list[SkillFile]:
    """All `.md` skills under `root` (non-recursive), sorted by name. Missing dir → empty list."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    out: list[SkillFile] = []
    for p in sorted(root.glob("*.md")):
        s = _skill_from_file(p)
        if s is not None:
            out.append(s)
    return out
