"""跨项目可移植关联：按名称重建角色、职业、组织映射。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.career import Career, CharacterCareer
from app.models.character import Character
from app.models.relationship import CharacterRelationship, Organization, RelationshipType
from app.services.organization_member_service import load_project_character_index

logger = get_logger(__name__)


@dataclass
class LinkImportStats:
    imported: int = 0
    skipped: int = 0
    failed: int = 0


def career_key(name: Optional[str], career_type: Optional[str]) -> Tuple[str, str]:
    return ((name or "").strip(), (career_type or "main").strip() or "main")


async def build_character_name_mapping(db: AsyncSession, project_id: str) -> Dict[str, str]:
    name_to_id, _ids, _org_ids = await load_project_character_index(db, project_id)
    return name_to_id


async def build_career_name_mapping(
    db: AsyncSession,
    project_id: str,
) -> Tuple[Dict[Tuple[str, str], str], Set[str]]:
    result = await db.execute(
        select(Career).where(Career.project_id == project_id)
    )
    mapping: Dict[Tuple[str, str], str] = {}
    ids: Set[str] = set()
    for career in result.scalars().all():
        mapping[career_key(career.name, career.type)] = career.id
        ids.add(career.id)
    return mapping, ids


async def build_organization_name_mapping(db: AsyncSession, project_id: str) -> Dict[str, str]:
    result = await db.execute(
        select(Organization.id, Character.name)
        .join(Character, Organization.character_id == Character.id)
        .where(Organization.project_id == project_id)
    )
    mapping: Dict[str, str] = {}
    for org_id, name in result:
        if name and name not in mapping:
            mapping[name] = org_id
    return mapping


def _norm_stages(stages: Any) -> str:
    if isinstance(stages, list):
        return json.dumps(stages, ensure_ascii=False, sort_keys=True)
    if not stages:
        return "[]"
    if isinstance(stages, str):
        try:
            return json.dumps(json.loads(stages), ensure_ascii=False, sort_keys=True)
        except Exception:
            return stages
    return str(stages)


def career_fingerprint(payload: Any) -> Tuple[str, str, int, str]:
    if isinstance(payload, Career):
        return (
            (payload.description or "").strip(),
            _norm_stages(payload.stages),
            int(payload.max_stage or 10),
            (payload.category or "").strip(),
        )
    return (
        str(payload.get("description") or "").strip(),
        _norm_stages(payload.get("stages")),
        int(payload.get("max_stage") or 10),
        str(payload.get("category") or "").strip(),
    )


async def upsert_careers_from_pack(
    db: AsyncSession,
    project_id: str,
    careers_data: Sequence[Dict[str, Any]],
    warnings: List[str],
) -> Dict[Tuple[str, str], str]:
    mapping, _ids = await build_career_name_mapping(db, project_id)
    if not careers_data:
        return mapping

    existing_by_key: Dict[Tuple[str, str], Career] = {}
    result = await db.execute(select(Career).where(Career.project_id == project_id))
    for career in result.scalars().all():
        existing_by_key[career_key(career.name, career.type)] = career

    for career_data in careers_data:
        if not isinstance(career_data, dict):
            continue
        name = (career_data.get("name") or "").strip()
        career_type = (career_data.get("type") or "main").strip() or "main"
        if not name:
            warnings.append("导入职业缺少 name，已跳过")
            continue
        key = career_key(name, career_type)
        existing = existing_by_key.get(key)
        if existing:
            if career_fingerprint(existing) != career_fingerprint(career_data):
                warnings.append(
                    f"目标项目已有职业「{name}」({career_type})，定义与导入包不一致，未覆盖，使用目标项目版本"
                )
            mapping[key] = existing.id
            continue
        career = Career(
            project_id=project_id,
            name=name,
            type=career_type,
            description=career_data.get("description"),
            category=career_data.get("category"),
            stages=career_data.get("stages") or "[]",
            max_stage=career_data.get("max_stage", 10) or 10,
            requirements=career_data.get("requirements"),
            special_abilities=career_data.get("special_abilities"),
            worldview_rules=career_data.get("worldview_rules"),
            attribute_bonuses=career_data.get("attribute_bonuses"),
            source=career_data.get("source") or "imported",
        )
        db.add(career)
        await db.flush()
        existing_by_key[key] = career
        mapping[key] = career.id
        logger.info(f"从角色包创建职业: {name} ({career_type})")
    return mapping


async def restore_character_careers(
    db: AsyncSession,
    project_id: str,
    items: Sequence[Dict[str, Any]],
    *,
    character_name_map: Dict[str, str],
    career_map: Dict[Tuple[str, str], str],
    project_career_ids: Set[str],
    warnings: List[str],
) -> LinkImportStats:
    stats = LinkImportStats()
    if not items:
        return stats

    char_ids = [cid for cid in character_name_map.values() if cid]
    existing_pairs: Set[Tuple[str, str]] = set()
    if char_ids:
        existing_result = await db.execute(
            select(CharacterCareer.character_id, CharacterCareer.career_id).where(
                CharacterCareer.character_id.in_(char_ids)
            )
        )
        existing_pairs = {(row[0], row[1]) for row in existing_result}

    touched_character_ids: Set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            stats.failed += 1
            continue
        try:
            character_name = (item.get("character_name") or "").strip()
            career_name = (item.get("career_name") or "").strip()
            career_type = (item.get("career_type") or "main").strip() or "main"
            raw_career_id = item.get("career_id")
            career_id = raw_career_id.strip() if isinstance(raw_career_id, str) else ""
            character_id = character_name_map.get(character_name)
            if not character_id:
                stats.skipped += 1
                warnings.append(f"未找到角色「{character_name or '未知'}」，职业关联未恢复")
                continue

            resolved_career_id = None
            if career_name:
                resolved_career_id = career_map.get(career_key(career_name, career_type))
                if not resolved_career_id:
                    label = "主职业" if career_type == "main" else "副职业"
                    warnings.append(
                        f"角色'{character_name}'引用{label}'{career_name}'，目标项目不存在该职业，职业关联未恢复"
                    )
                    stats.skipped += 1
                    continue
            elif career_id:
                if career_id in project_career_ids:
                    resolved_career_id = career_id
                else:
                    warnings.append(
                        f"角色'{character_name}'的职业ID不属于当前项目，职业关联未恢复"
                    )
                    stats.skipped += 1
                    continue
            else:
                warnings.append(f"角色'{character_name or '未知'}'的职业关联缺少 career_name")
                stats.skipped += 1
                continue

            if (character_id, resolved_career_id) in existing_pairs:
                stats.skipped += 1
                warnings.append(f"角色「{character_name}」与职业关联已存在，已跳过")
                continue

            db.add(
                CharacterCareer(
                    character_id=character_id,
                    career_id=resolved_career_id,
                    career_type=career_type,
                    current_stage=item.get("current_stage", 1) or 1,
                    stage_progress=item.get("stage_progress", 0) or 0,
                    started_at=item.get("started_at"),
                    reached_current_stage_at=item.get("reached_current_stage_at"),
                    notes=item.get("notes"),
                )
            )
            existing_pairs.add((character_id, resolved_career_id))
            touched_character_ids.add(character_id)
            stats.imported += 1
        except Exception as exc:
            stats.failed += 1
            logger.warning(f"恢复职业关联失败: {exc}")
            warnings.append(f"恢复职业关联失败: {exc}")

    if stats.imported:
        await db.flush()
        await _sync_character_career_fields(db, touched_character_ids)
    return stats


async def _sync_character_career_fields(db: AsyncSession, character_ids: Iterable[str]) -> None:
    ids = [cid for cid in set(character_ids) if cid]
    if not ids:
        return
    link_result = await db.execute(
        select(CharacterCareer).where(CharacterCareer.character_id.in_(ids))
    )
    by_character: Dict[str, List[CharacterCareer]] = {}
    for link in link_result.scalars().all():
        by_character.setdefault(link.character_id, []).append(link)

    char_result = await db.execute(select(Character).where(Character.id.in_(ids)))
    for character in char_result.scalars().all():
        links = by_character.get(character.id, [])
        mains = [link for link in links if link.career_type == "main"]
        subs = [link for link in links if link.career_type == "sub"]
        if mains:
            character.main_career_id = mains[0].career_id
            character.main_career_stage = mains[0].current_stage
        if subs:
            character.sub_careers = json.dumps(
                [{"career_id": link.career_id, "stage": link.current_stage} for link in subs[:2]],
                ensure_ascii=False,
            )


async def restore_relationships(
    db: AsyncSession,
    project_id: str,
    items: Sequence[Dict[str, Any]],
    *,
    character_name_map: Dict[str, str],
    warnings: List[str],
) -> LinkImportStats:
    stats = LinkImportStats()
    if not items:
        return stats

    existing_result = await db.execute(
        select(
            CharacterRelationship.character_from_id,
            CharacterRelationship.character_to_id,
            CharacterRelationship.relationship_name,
        ).where(CharacterRelationship.project_id == project_id)
    )
    existing = {
        (row[0], row[1], row[2] or "")
        for row in existing_result
    }

    type_result = await db.execute(select(RelationshipType))
    type_by_name = {row.name: row.id for row in type_result.scalars().all() if row.name}

    for item in items:
        if not isinstance(item, dict):
            stats.failed += 1
            continue
        try:
            source_name = (item.get("source_name") or "").strip()
            target_name = (item.get("target_name") or "").strip()
            relationship_name = item.get("relationship_name")
            rel_name = relationship_name.strip() if isinstance(relationship_name, str) else ""
            source_id = character_name_map.get(source_name)
            target_id = character_name_map.get(target_name)
            if not source_id or not target_id:
                missing = source_name if not source_id else target_name
                warnings.append(f"人物关系「{source_name}->{target_name}」缺少角色「{missing}」，已跳过")
                stats.skipped += 1
                continue
            key = (source_id, target_id, rel_name)
            if key in existing:
                warnings.append(f"人物关系「{source_name}->{target_name}」（{rel_name or '未命名'}）已存在，已跳过")
                stats.skipped += 1
                continue
            db.add(
                CharacterRelationship(
                    project_id=project_id,
                    character_from_id=source_id,
                    character_to_id=target_id,
                    relationship_name=rel_name or None,
                    relationship_type_id=type_by_name.get(rel_name) if rel_name else None,
                    intimacy_level=item.get("intimacy_level", 50) if item.get("intimacy_level") is not None else 50,
                    status=item.get("status") or "active",
                    description=item.get("description"),
                    started_at=item.get("started_at"),
                    source="imported",
                )
            )
            existing.add(key)
            stats.imported += 1
        except Exception as exc:
            stats.failed += 1
            logger.warning(f"恢复人物关系失败: {exc}")
            warnings.append(f"恢复人物关系失败: {exc}")

    if stats.imported:
        await db.flush()
    return stats


def collect_legacy_career_links(characters_data: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把旧版 main_career_id / sub_careers 转成名称优先、UUID 兜底的关联项。"""
    links: List[Dict[str, Any]] = []
    for char_data in characters_data:
        if not isinstance(char_data, dict) or char_data.get("is_organization"):
            continue
        name = char_data.get("name")
        if not name:
            continue
        main_id = char_data.get("main_career_id")
        if main_id:
            links.append({
                "character_name": name,
                "career_id": main_id,
                "career_type": "main",
                "current_stage": char_data.get("main_career_stage") or 1,
            })
        raw_subs = char_data.get("sub_careers")
        if not raw_subs:
            continue
        try:
            subs = json.loads(raw_subs) if isinstance(raw_subs, str) else raw_subs
        except Exception:
            continue
        if not isinstance(subs, list):
            continue
        for sub in subs[:2]:
            if not isinstance(sub, dict):
                continue
            career_id = sub.get("career_id")
            if career_id:
                links.append({
                    "character_name": name,
                    "career_id": career_id,
                    "career_type": "sub",
                    "current_stage": sub.get("stage") or 1,
                })
    return links
