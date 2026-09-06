"""Skill discovery — scan a directory of human-editable Markdown skills and parse their metadata.

**Layered knowledge** (production upgrade): a skill is more than a title + body. Real skills have a
CHEAP summary (always rendered when the skill is selected) and expensive DEEP sections (rendered
only when the caller can afford the tokens and the task needs the depth). The Markdown structure is:

    ---
    name: Tailwind CSS
    tags: [tailwind, css, frontend]
    dependencies: [css]          # other skills this one composes with
    conflicts: []                # skills that should not co-activate
    version_hint: tailwind@3     # what "current" typically means for this skill
    project_detect:              # files whose presence indicates this skill applies
      - tailwind.config.js
      - tailwind.config.ts
      - postcss.config.js
    summary: One-paragraph operational summary (~2-4 sentences).
    ---

    # Tailwind CSS

    Introduction / rationale (always rendered with the summary).

    ## Setup
    ...

    ## Deep dive: variants and arbitrary values
    ...

    ## Anti-patterns
    ...

The frontmatter is parsed tolerantly WITHOUT a YAML dependency. Sections are extracted from `## `
headers so the retriever can pull only the sections a query needs. A file with no frontmatter and
no sections still works — its whole body is the summary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from sali.obs.log import get_logger

log = get_logger("sali.skills")

_FRONTMATTER = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(slots=True)
class SkillFile:
    name: str                       # slug — the filename stem (stable identity)
    title: str                      # human title (frontmatter `name`, else the slug)
    path: str
    tags: list[str] = field(default_factory=list)
    content: str = ""               # the markdown body (frontmatter stripped)
    content_hash: str = ""          # sha256 of the raw file — the snapshot version key

    # Layered knowledge (added for the production skill system).
    summary: str = ""               # a short paragraph — always rendered when selected
    sections: dict[str, str] = field(default_factory=dict)   # h2-title → section body
    dependencies: list[str] = field(default_factory=list)    # skills this one composes with
    conflicts: list[str] = field(default_factory=list)       # skills that should not co-activate
    version_hint: str = ""          # e.g. "laravel@11", "tailwind@3" — what "current" means
    project_detect: list[str] = field(default_factory=list)  # relative paths signalling the skill fits

    @property
    def has_layered_content(self) -> bool:
        return bool(self.summary) or bool(self.sections)


def _parse_list_value(val: str) -> list[str]:
    """Parse a frontmatter list value in either inline (`[a, b]`) or bare CSV form."""
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        return [t.strip().strip("\"'") for t in val[1:-1].split(",") if t.strip()]
    if val:
        return [t.strip().strip("\"'") for t in val.split(",") if t.strip()]
    return []


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split `--- ... ---` frontmatter from the body. Returns (meta, body). Tolerant: a file with
    no frontmatter returns ({}, text). Recognized keys: name, tags, dependencies, conflicts,
    version_hint, project_detect, summary. Unknown keys are ignored (forward-compat).
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict[str, object] = {}
    block_list_key: str | None = None
    block_list: list[str] = []

    def flush_block() -> None:
        nonlocal block_list_key, block_list
        if block_list_key is not None:
            meta[block_list_key] = block_list
        block_list_key = None
        block_list = []

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_block()
            continue
        if block_list_key is not None and stripped.startswith("- "):
            block_list.append(stripped[2:].strip().strip("\"'"))
            continue
        flush_block()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "name":
            meta["name"] = val.strip("\"'")
        elif key == "summary":
            meta["summary"] = val.strip("\"'")
        elif key == "version_hint":
            meta["version_hint"] = val.strip("\"'")
        elif key in ("tags", "dependencies", "conflicts", "project_detect"):
            parsed = _parse_list_value(val)
            if parsed:
                meta[key] = parsed
            else:
                block_list_key = key
                block_list = []
    flush_block()
    return meta, body.strip()


def _split_sections(body: str) -> tuple[str, dict[str, str]]:
    """Split a Markdown body into (preamble, {section_title: section_body}) on `## ` H2 headings.

    The preamble is everything before the first H2 — typically the skill's own intro paragraph +
    high-level rationale, which is always rendered alongside the summary. Each H2 section body is
    everything from the H2 up to (but excluding) the next H2, with the heading removed. Sections
    are returned in file order via an ordered dict semantics (Python 3.7+ dict insertion order).
    """
    if not body:
        return "", {}
    matches = list(_H2.finditer(body))
    if not matches:
        return body.strip(), {}
    preamble = body[: matches[0].start()].strip()
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_body = body[start:end].strip()
        # Keep the original title untouched — the retriever indexes by lowercased title so casing
        # doesn't matter, and the renderer shows the human title verbatim.
        sections[title] = section_body
    return preamble, sections


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
    deps = meta.get("dependencies") or []
    conflicts = meta.get("conflicts") or []
    project_detect = meta.get("project_detect") or []
    summary = str(meta.get("summary") or "")
    version_hint = str(meta.get("version_hint") or "")

    preamble, sections = _split_sections(body or raw.strip())
    # If the frontmatter didn't supply a summary, use the preamble (first paragraph before the
    # first H2). Cap it at a reasonable size so a skill with a long intro doesn't blow the budget.
    if not summary:
        summary = preamble[:600].rstrip() + ("…" if len(preamble) > 600 else "")

    return SkillFile(
        name=slug,
        title=str(meta.get("name") or slug),
        path=str(path),
        tags=tags,
        content=body or raw.strip(),
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        summary=summary,
        sections=sections,
        dependencies=[str(d).lower() for d in deps if isinstance(d, str)],
        conflicts=[str(c).lower() for c in conflicts if isinstance(c, str)],
        version_hint=version_hint,
        project_detect=[str(p) for p in project_detect if isinstance(p, str)],
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
