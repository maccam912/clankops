from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DEFAULT_SKILLS_DIR = "skills"
_SKILL_FILENAME = "SKILL.md"

# Limits are intentionally conservative; skill bodies can be loaded on-demand via tool.
_MAX_SKILLS_LISTED = 200
_MAX_SKILL_FILE_CHARS_FOR_SUMMARY = 40_000


@dataclass(frozen=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    path: Path


def normalize_skill_id(raw: str) -> str:
    """Normalize a user-provided skill id to a safe, filesystem-friendly slug."""
    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        raise ValueError("Skill id is empty.")
    if not _SKILL_ID_RE.fullmatch(text):
        raise ValueError(
            "Invalid skill id. Use 1-64 chars: lowercase letters, numbers, '-' and '_' "
            "(must start with a letter/number)."
        )
    return text


def resolve_skills_dir(workspace_root: Path) -> Path:
    """Resolve SKILLS_DIR.

    If SKILLS_DIR is relative, it's resolved under workspace_root.
    If it's absolute, it's used as-is (allows sharing skills across projects).
    """
    raw = os.environ.get("SKILLS_DIR", _DEFAULT_SKILLS_DIR).strip() or _DEFAULT_SKILLS_DIR
    user_path = Path(raw)
    return user_path.resolve() if user_path.is_absolute() else (workspace_root / user_path).resolve()


def _skill_dir(skills_dir: Path, skill_id: str) -> Path:
    return skills_dir / skill_id


def _skill_file(skills_dir: Path, skill_id: str) -> Path:
    return _skill_dir(skills_dir, skill_id) / _SKILL_FILENAME


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse very small YAML-like frontmatter: key: value lines between --- delimiters."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    fm_lines: list[str] = []
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
        fm_lines.append(lines[i])

    if end_idx is None:
        return {}, text

    fm: dict[str, str] = {}
    for line in fm_lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            fm[key] = value

    body = "\n".join(lines[end_idx + 1 :]).lstrip("\n")
    return fm, body


def _infer_title_and_description(body: str) -> tuple[str, str]:
    title = ""
    description = ""
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not title and stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            continue
        if not description and not stripped.startswith("#"):
            description = stripped
            break
    return title, description


def read_skill_meta(workspace_root: Path, skill_id: str) -> SkillMeta:
    skills_dir = resolve_skills_dir(workspace_root)
    sid = normalize_skill_id(skill_id)
    path = _skill_file(skills_dir, sid)
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {sid}")
    text = path.read_text(encoding="utf-8", errors="replace")
    text = text[:_MAX_SKILL_FILE_CHARS_FOR_SUMMARY]

    fm, body = _parse_frontmatter(text)
    fm_name = (fm.get("name") or "").strip()
    fm_desc = (fm.get("description") or "").strip()
    title, inferred_desc = _infer_title_and_description(body)

    name = fm_name or title or sid
    description = fm_desc or inferred_desc or ""
    return SkillMeta(skill_id=sid, name=name, description=description, path=path)


def list_skills(workspace_root: Path, max_skills: int = _MAX_SKILLS_LISTED) -> list[SkillMeta]:
    skills_dir = resolve_skills_dir(workspace_root)
    if not skills_dir.exists():
        return []

    safe_max = max(1, min(int(max_skills), _MAX_SKILLS_LISTED))
    metas: list[SkillMeta] = []
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        sid = child.name
        try:
            sid = normalize_skill_id(sid)
        except ValueError:
            continue
        skill_path = _skill_file(skills_dir, sid)
        if not skill_path.exists() or not skill_path.is_file():
            continue
        try:
            metas.append(read_skill_meta(workspace_root, sid))
        except OSError:
            continue
        if len(metas) >= safe_max:
            break
    return metas


def read_skill_markdown(workspace_root: Path, skill_id: str, max_chars: int = 12_000) -> dict[str, object]:
    skills_dir = resolve_skills_dir(workspace_root)
    sid = normalize_skill_id(skill_id)
    path = _skill_file(skills_dir, sid)
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {sid}")
    text = path.read_text(encoding="utf-8", errors="replace")
    safe_max = max(200, min(int(max_chars), 100_000))
    truncated = len(text) > safe_max
    return {
        "skill_id": sid,
        "path": str(path),
        "truncated": truncated,
        "content": text[:safe_max],
    }


def upsert_skill(
    workspace_root: Path,
    skill_id: str,
    description: str,
    body_markdown: str,
    *,
    display_name: str | None = None,
    overwrite: bool = True,
) -> dict[str, object]:
    skills_dir = resolve_skills_dir(workspace_root)
    sid = normalize_skill_id(skill_id)

    desc = (description or "").strip()
    if not desc:
        raise ValueError("description is required.")
    body = (body_markdown or "").strip()
    if not body:
        raise ValueError("body_markdown is required.")

    skill_dir = _skill_dir(skills_dir, sid)
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = _skill_file(skills_dir, sid)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Skill already exists: {sid}")

    name = (display_name or "").strip()
    if not name:
        name = sid

    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{body}\n"
    )
    path.write_text(content, encoding="utf-8")
    meta = read_skill_meta(workspace_root, sid)
    return {
        "ok": True,
        "skill_id": meta.skill_id,
        "name": meta.name,
        "description": meta.description,
        "path": str(meta.path),
    }


def delete_skill(workspace_root: Path, skill_id: str, *, confirm: bool = False) -> dict[str, object]:
    if not confirm:
        raise ValueError("Refusing to delete without confirm=True.")
    skills_dir = resolve_skills_dir(workspace_root)
    sid = normalize_skill_id(skill_id)
    target = _skill_dir(skills_dir, sid)
    if not target.exists():
        return {"ok": True, "skill_id": sid, "deleted": False, "reason": "not found"}
    if not target.is_dir():
        raise ValueError("Skill path exists but is not a directory.")
    shutil.rmtree(target)
    return {"ok": True, "skill_id": sid, "deleted": True}


def format_skills_for_system_prompt(workspace_root: Path, *, max_chars: int = 1800) -> str:
    """Build a compact SKILLS section suitable for a system prompt."""
    skills_dir = resolve_skills_dir(workspace_root)
    try:
        metas = list_skills(workspace_root)
    except Exception:
        metas = []

    header = (
        "\n\nSKILLS\n"
        "Skills are reusable instruction packs stored as markdown at:\n"
        f"- {skills_dir / '<skill_id>' / _SKILL_FILENAME}\n"
        "Use read_skill(skill_id) to load a skill's full markdown only when needed.\n"
        "Use list_skills() to refresh the list after creating/editing skills.\n\n"
        "Available skills (skill_id: description):\n"
    )

    if not metas:
        body = "- (none installed)\n"
        return header + body

    lines: list[str] = []
    used = 0
    for meta in metas:
        desc = meta.description.strip().replace("\n", " ")
        if not desc:
            desc = "(no description)"
        label = (
            f"{meta.skill_id} ({meta.name})"
            if meta.name and meta.name.strip() and meta.name.strip() != meta.skill_id
            else meta.skill_id
        )
        line = f"- {label}: {desc}\n"
        if used + len(line) > max_chars:
            lines.append("- ... (truncated)\n")
            break
        lines.append(line)
        used += len(line)
    return header + "".join(lines)
