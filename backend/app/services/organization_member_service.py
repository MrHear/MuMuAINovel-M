"""组织成员关联：可移植导入导出、批量添加、成员计数同步。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logger import get_logger
from app.models.character import Character
from app.models.relationship import Organization, OrganizationMember

logger = get_logger(__name__)


@dataclass(frozen=True)
class MemberResolveResult:
    character_id: Optional[str]
    warning: Optional[str] = None


@dataclass
class BatchMemberResult:
    added: List[OrganizationMember] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    member_count: int = 0


def resolve_portable_character(
    member_data: Dict[str, Any],
    *,
    name_to_id: Dict[str, str],
    project_character_ids: Set[str],
) -> MemberResolveResult:
    """按名称优先、再兼容旧 character_id 解析目标项目中的角色。"""
    raw_name = member_data.get("character_name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    raw_id = member_data.get("character_id")
    character_id = raw_id.strip() if isinstance(raw_id, str) else ""

    if name:
        matched = name_to_id.get(name)
        if matched:
            return MemberResolveResult(character_id=matched)
        return MemberResolveResult(
            character_id=None,
            warning=f"未找到名为「{name}」的角色，已跳过该成员",
        )

    if character_id:
        if character_id in project_character_ids:
            return MemberResolveResult(character_id=character_id)
        return MemberResolveResult(
            character_id=None,
            warning=f"旧 character_id 不属于当前项目，已忽略: {character_id}",
        )

    return MemberResolveResult(
        character_id=None,
        warning="成员缺少 character_name 和可用的 character_id",
    )


def build_portable_member_payload(
    member: OrganizationMember,
    character_name: str,
) -> Dict[str, Any]:
    """导出可跨项目恢复的组织成员数据，不以 character_id 作为关联依据。"""
    return {
        "character_name": character_name,
        "position": member.position or "成员",
        "rank": member.rank or 0,
        "loyalty": member.loyalty if member.loyalty is not None else 50,
        "contribution": member.contribution or 0,
        "status": member.status or "active",
        "joined_at": member.joined_at,
        "source": member.source or "imported",
        "notes": member.notes,
    }


async def load_project_character_index(
    db: AsyncSession,
    project_id: str,
) -> Tuple[Dict[str, str], Set[str], Set[str]]:
    """批量构建 name->id、项目角色ID集合、组织类型角色ID集合。"""
    result = await db.execute(
        select(Character.id, Character.name, Character.is_organization).where(
            Character.project_id == project_id
        )
    )
    name_to_id: Dict[str, str] = {}
    project_character_ids: Set[str] = set()
    organization_character_ids: Set[str] = set()
    for row in result:
        project_character_ids.add(row.id)
        if row.is_organization:
            organization_character_ids.add(row.id)
        if row.name not in name_to_id:
            name_to_id[row.name] = row.id
    return name_to_id, project_character_ids, organization_character_ids


async def load_character_name_map(
    db: AsyncSession,
    character_ids: Iterable[str],
) -> Dict[str, str]:
    ids = [cid for cid in set(character_ids) if cid]
    if not ids:
        return {}
    result = await db.execute(
        select(Character.id, Character.name).where(Character.id.in_(ids))
    )
    return {row.id: row.name for row in result}


async def sync_organization_member_count(
    organization: Organization,
    db: AsyncSession,
) -> int:
    """按 organization_members 实际行数同步 member_count。"""
    result = await db.execute(
        select(func.count()).select_from(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id
        )
    )
    actual_count = int(result.scalar_one() or 0)
    if organization.member_count != actual_count:
        logger.info(
            f"同步组织 {organization.id} 成员计数: {organization.member_count} -> {actual_count}"
        )
        organization.member_count = actual_count
        await db.flush()
    return actual_count


async def sync_organization_member_count_by_id(
    org_id: str,
    db: AsyncSession,
) -> int:
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = result.scalar_one_or_none()
    if not organization:
        return 0
    return await sync_organization_member_count(organization, db)


async def restore_organization_members(
    db: AsyncSession,
    organization: Organization,
    members_data: Sequence[Dict[str, Any]],
    *,
    project_id: str,
    warnings: List[str],
    org_label: Optional[str] = None,
    name_to_id: Optional[Dict[str, str]] = None,
    project_character_ids: Optional[Set[str]] = None,
    organization_character_ids: Optional[Set[str]] = None,
) -> int:
    """按可移植字段恢复成员关系。单条失败只记 warning，不抛出。"""
    if not members_data:
        return 0

    if name_to_id is None or project_character_ids is None or organization_character_ids is None:
        name_to_id, project_character_ids, organization_character_ids = (
            await load_project_character_index(db, project_id)
        )

    existing_result = await db.execute(
        select(OrganizationMember.character_id).where(
            OrganizationMember.organization_id == organization.id
        )
    )
    existing_ids = {row[0] for row in existing_result}

    added = 0
    label = org_label or organization.id
    for member_data in members_data:
        if not isinstance(member_data, dict):
            warnings.append(f"组织「{label}」存在无效成员数据，已跳过")
            continue
        try:
            resolved = resolve_portable_character(
                member_data,
                name_to_id=name_to_id,
                project_character_ids=project_character_ids,
            )
            display = member_data.get("character_name") or member_data.get("character_id") or "未知"
            if not resolved.character_id:
                if resolved.warning:
                    warnings.append(f"组织「{label}」成员 {display}: {resolved.warning}")
                continue
            if resolved.character_id in organization_character_ids:
                warnings.append(f"组织「{label}」不能将组织「{display}」添加为成员，已跳过")
                continue
            if resolved.character_id in existing_ids:
                warnings.append(f"角色「{display}」已属于组织「{label}」，已跳过")
                continue

            db.add(
                OrganizationMember(
                    organization_id=organization.id,
                    character_id=resolved.character_id,
                    position=member_data.get("position") or "成员",
                    rank=member_data.get("rank", 0) or 0,
                    loyalty=member_data.get("loyalty", 50) if member_data.get("loyalty") is not None else 50,
                    contribution=member_data.get("contribution", 0) or 0,
                    status=member_data.get("status") or "active",
                    joined_at=member_data.get("joined_at"),
                    notes=member_data.get("notes"),
                    source=member_data.get("source") or "imported",
                )
            )
            existing_ids.add(resolved.character_id)
            added += 1
        except Exception as exc:
            logger.warning(f"恢复组织「{label}」成员失败: {exc}")
            warnings.append(f"组织「{label}」恢复成员失败: {exc}")

    if added:
        await db.flush()
    await sync_organization_member_count(organization, db)
    return added


def _member_item(
    member_data: Dict[str, Any],
    *,
    character_id: Optional[str] = None,
    character_name: Optional[str] = None,
    reason: str,
) -> Dict[str, Any]:
    return {
        "character_id": character_id or member_data.get("character_id"),
        "character_name": character_name,
        "reason": reason,
    }


async def add_organization_members_batch(
    db: AsyncSession,
    organization: Organization,
    members: Sequence[Dict[str, Any]],
) -> BatchMemberResult:
    """批量添加组织成员。单条异常不影响其他合法成员。"""
    result = BatchMemberResult()
    if not members:
        result.member_count = await sync_organization_member_count(organization, db)
        return result

    character_ids = [str(item.get("character_id") or "") for item in members if item.get("character_id")]
    characters: Dict[str, Character] = {}
    if character_ids:
        char_result = await db.execute(select(Character).where(Character.id.in_(character_ids)))
        characters = {char.id: char for char in char_result.scalars().all()}

    existing_result = await db.execute(
        select(OrganizationMember.character_id).where(
            OrganizationMember.organization_id == organization.id
        )
    )
    existing_ids = {row[0] for row in existing_result}

    for member_data in members:
        character_id = member_data.get("character_id") if isinstance(member_data, dict) else None
        if not character_id:
            result.errors.append(_member_item(member_data or {}, reason="缺少 character_id"))
            continue
        try:
            character = characters.get(character_id)
            if not character:
                result.errors.append(_member_item(member_data, character_id=character_id, reason="角色不存在"))
                continue
            if character.project_id != organization.project_id:
                result.errors.append(
                    _member_item(
                        member_data,
                        character_id=character_id,
                        character_name=character.name,
                        reason="角色不属于该组织所在项目",
                    )
                )
                continue
            if character.is_organization:
                result.errors.append(
                    _member_item(
                        member_data,
                        character_id=character_id,
                        character_name=character.name,
                        reason="不能将组织添加为成员",
                    )
                )
                continue
            if character_id in existing_ids:
                result.skipped.append(
                    _member_item(
                        member_data,
                        character_id=character_id,
                        character_name=character.name,
                        reason="该角色已在组织中",
                    )
                )
                continue
            if not member_data.get("position"):
                result.errors.append(
                    _member_item(
                        member_data,
                        character_id=character_id,
                        character_name=character.name,
                        reason="缺少职位",
                    )
                )
                continue

            db_member = OrganizationMember(
                organization_id=organization.id,
                character_id=character_id,
                position=member_data.get("position"),
                rank=member_data.get("rank", 0) or 0,
                loyalty=member_data.get("loyalty", 50) if member_data.get("loyalty") is not None else 50,
                contribution=member_data.get("contribution", 0) or 0,
                status=member_data.get("status") or "active",
                joined_at=member_data.get("joined_at"),
                left_at=member_data.get("left_at"),
                notes=member_data.get("notes"),
                source="manual",
            )
            db.add(db_member)
            existing_ids.add(character_id)
            result.added.append(db_member)
        except Exception as exc:
            logger.warning(f"批量添加成员失败 character_id={character_id}: {exc}")
            result.errors.append(
                _member_item(member_data, character_id=character_id, reason=str(exc))
            )

    if result.added:
        await db.flush()
        for member in result.added:
            await db.refresh(member)

    result.member_count = await sync_organization_member_count(organization, db)
    return result
