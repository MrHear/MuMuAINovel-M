"""组织成员导入导出与批量添加的行为测试。"""
import unittest
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import Organization, OrganizationMember
from app.services.import_export_service import ImportExportService
from app.services.organization_member_service import (
    add_organization_members_batch,
    build_portable_member_payload,
    resolve_portable_character,
    restore_organization_members,
    sync_organization_member_count,
)


def _uid() -> str:
    return str(uuid.uuid4())


class ResolvePortableCharacterTests(unittest.TestCase):
    def test_prefers_character_name(self):
        result = resolve_portable_character(
            {
                "character_name": "沈青禾",
                "character_id": "old-foreign-id",
            },
            name_to_id={"沈青禾": "new-id"},
            project_character_ids={"new-id"},
        )
        self.assertEqual(result.character_id, "new-id")
        self.assertIsNone(result.warning)

    def test_falls_back_to_character_id_in_target_project(self):
        result = resolve_portable_character(
            {"character_id": "same-project-id"},
            name_to_id={},
            project_character_ids={"same-project-id"},
        )
        self.assertEqual(result.character_id, "same-project-id")
        self.assertIsNone(result.warning)

    def test_rejects_foreign_character_id(self):
        result = resolve_portable_character(
            {"character_id": "foreign-id"},
            name_to_id={},
            project_character_ids={"local-id"},
        )
        self.assertIsNone(result.character_id)
        self.assertIn("不属于当前项目", result.warning or "")

    def test_missing_name_adds_warning(self):
        result = resolve_portable_character(
            {"position": "弟子"},
            name_to_id={"沈青禾": "id-1"},
            project_character_ids={"id-1"},
        )
        self.assertIsNone(result.character_id)
        self.assertIn("缺少", result.warning or "")

    def test_unknown_name_adds_warning(self):
        result = resolve_portable_character(
            {"character_name": "不存在的人"},
            name_to_id={"沈青禾": "id-1"},
            project_character_ids={"id-1"},
        )
        self.assertIsNone(result.character_id)
        self.assertIn("不存在的人", result.warning or "")


class PortableMemberPayloadTests(unittest.TestCase):
    def test_export_payload_uses_character_name(self):
        member = OrganizationMember(
            id=_uid(),
            organization_id=_uid(),
            character_id=_uid(),
            position="内门弟子",
            rank=4,
            loyalty=72,
            contribution=0,
            status="active",
            joined_at="六年前",
            source="manual",
        )
        payload = build_portable_member_payload(member, "沈青禾")
        self.assertEqual(payload["character_name"], "沈青禾")
        self.assertNotIn("character_id", payload)
        self.assertEqual(payload["position"], "内门弟子")
        self.assertEqual(payload["rank"], 4)
        self.assertEqual(payload["source"], "manual")


class OrganizationMemberIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _session(self) -> AsyncSession:
        return self.session_factory()

    async def _seed_project(self, db: AsyncSession, title: str = "源项目") -> Project:
        project = Project(id=_uid(), user_id="user-1", title=title)
        db.add(project)
        await db.flush()
        return project

    async def _seed_character(
        self,
        db: AsyncSession,
        project_id: str,
        name: str,
        *,
        is_organization: bool = False,
        character_id: str | None = None,
    ) -> Character:
        character = Character(
            id=character_id or _uid(),
            project_id=project_id,
            name=name,
            is_organization=is_organization,
            role_type="supporting",
        )
        db.add(character)
        await db.flush()
        return character

    async def _seed_org(
        self,
        db: AsyncSession,
        project_id: str,
        org_char: Character,
    ) -> Organization:
        org = Organization(
            id=_uid(),
            character_id=org_char.id,
            project_id=project_id,
            member_count=0,
            power_level=50,
        )
        db.add(org)
        await db.flush()
        return org

    async def test_same_project_single_member_add(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            member_char = await self._seed_character(db, project.id, "沈青禾")

            result = await add_organization_members_batch(
                db,
                org,
                [{"character_id": member_char.id, "position": "内门弟子"}],
            )
            await db.commit()

            self.assertEqual(len(result.added), 1)
            self.assertEqual(result.member_count, 1)
            self.assertEqual(org.member_count, 1)

    async def test_batch_add_three_members(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            names = ["甲", "乙", "丙"]
            members = [
                {"character_id": (await self._seed_character(db, project.id, name)).id, "position": "弟子"}
                for name in names
            ]

            result = await add_organization_members_batch(db, org, members)
            await db.commit()

            self.assertEqual(len(result.added), 3)
            self.assertEqual(result.errors, [])
            self.assertEqual(result.member_count, 3)

    async def test_batch_skips_duplicate_member(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            member_char = await self._seed_character(db, project.id, "沈青禾")
            first = await add_organization_members_batch(
                db, org, [{"character_id": member_char.id, "position": "弟子"}]
            )
            second = await add_organization_members_batch(
                db, org, [{"character_id": member_char.id, "position": "长老"}]
            )
            await db.commit()

            self.assertEqual(len(first.added), 1)
            self.assertEqual(len(second.added), 0)
            self.assertEqual(len(second.skipped), 1)
            self.assertEqual(second.member_count, 1)

    async def test_batch_rejects_organization_character(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            other_org = await self._seed_character(db, project.id, "魔教", is_organization=True)

            result = await add_organization_members_batch(
                db, org, [{"character_id": other_org.id, "position": "成员"}]
            )
            await db.commit()

            self.assertEqual(result.added, [])
            self.assertEqual(len(result.errors), 1)
            self.assertIn("组织", result.errors[0]["reason"])

    async def test_batch_rejects_other_project_character(self):
        async with await self._session() as db:
            project_a = await self._seed_project(db, "项目A")
            project_b = await self._seed_project(db, "项目B")
            org_char = await self._seed_character(db, project_a.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project_a.id, org_char)
            foreign = await self._seed_character(db, project_b.id, "外人")

            result = await add_organization_members_batch(
                db, org, [{"character_id": foreign.id, "position": "弟子"}]
            )
            await db.commit()

            self.assertEqual(result.added, [])
            self.assertEqual(len(result.errors), 1)
            self.assertIn("项目", result.errors[0]["reason"])

    async def test_export_import_restores_members_by_name(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            org_char = await self._seed_character(db, source.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, source.id, org_char)
            member_char = await self._seed_character(db, source.id, "沈青禾")
            await add_organization_members_batch(
                db,
                org,
                [{
                    "character_id": member_char.id,
                    "position": "内门弟子",
                    "rank": 4,
                    "loyalty": 72,
                    "joined_at": "六年前",
                }],
            )
            await db.commit()

            exported = await ImportExportService.export_characters(
                [org_char.id, member_char.id],
                db,
            )
            org_payload = next(item for item in exported["data"] if item["is_organization"])
            self.assertIn("organization_members_data", org_payload)
            self.assertEqual(org_payload["organization_members_data"][0]["character_name"], "沈青禾")
            self.assertNotIn("character_id", org_payload["organization_members_data"][0])

            target = await self._seed_project(db, "目标")
            result = await ImportExportService.import_characters(
                exported,
                target.id,
                "user-1",
                db,
            )
            self.assertTrue(result["success"], result)

            org_row = (
                await db.execute(
                    select(Organization).join(Character, Organization.character_id == Character.id).where(
                        Character.project_id == target.id,
                        Character.name == "青云宗",
                    )
                )
            ).scalar_one()
            members = (
                await db.execute(
                    select(OrganizationMember).where(OrganizationMember.organization_id == org_row.id)
                )
            ).scalars().all()
            self.assertEqual(len(members), 1)
            self.assertEqual(org_row.member_count, 1)
            member_name = (
                await db.execute(select(Character).where(Character.id == members[0].character_id))
            ).scalar_one()
            self.assertEqual(member_name.name, "沈青禾")
            self.assertEqual(members[0].position, "内门弟子")

    async def test_old_character_id_json_does_not_fail(self):
        async with await self._session() as db:
            target = await self._seed_project(db, "目标")
            existing = await self._seed_character(db, target.id, "沈青禾")
            payload = {
                "version": "1.1.0",
                "export_time": datetime.utcnow().isoformat(),
                "export_type": "characters",
                "count": 1,
                "data": [
                    {
                        "name": "青云宗",
                        "is_organization": True,
                        "organization_members_data": [
                            {
                                "character_id": existing.id,
                                "position": "弟子",
                                "rank": 0,
                                "loyalty": 50,
                                "contribution": 0,
                                "status": "active",
                            }
                        ],
                    }
                ],
            }
            result = await ImportExportService.import_characters(payload, target.id, "user-1", db)
            self.assertTrue(result["success"], result)

            org_row = (
                await db.execute(
                    select(Organization).join(Character, Organization.character_id == Character.id).where(
                        Character.project_id == target.id,
                        Character.name == "青云宗",
                    )
                )
            ).scalar_one()
            members = (
                await db.execute(
                    select(OrganizationMember).where(OrganizationMember.organization_id == org_row.id)
                )
            ).scalars().all()
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0].character_id, existing.id)

    async def test_old_foreign_character_id_does_not_bind(self):
        async with await self._session() as db:
            other = await self._seed_project(db, "别人的项目")
            foreign = await self._seed_character(db, other.id, "同名迷惑")
            target = await self._seed_project(db, "目标")
            await self._seed_character(db, target.id, "本地角色")
            payload = {
                "version": "1.1.0",
                "export_time": datetime.utcnow().isoformat(),
                "export_type": "characters",
                "count": 1,
                "data": [
                    {
                        "name": "青云宗",
                        "is_organization": True,
                        "organization_members_data": [
                            {
                                "character_id": foreign.id,
                                "position": "弟子",
                            }
                        ],
                    }
                ],
            }
            result = await ImportExportService.import_characters(payload, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            self.assertTrue(any("不属于当前项目" in w for w in result["warnings"]))

            org_row = (
                await db.execute(
                    select(Organization).join(Character, Organization.character_id == Character.id).where(
                        Character.project_id == target.id,
                        Character.name == "青云宗",
                    )
                )
            ).scalar_one()
            members = (
                await db.execute(
                    select(OrganizationMember).where(OrganizationMember.organization_id == org_row.id)
                )
            ).scalars().all()
            self.assertEqual(members, [])
            self.assertEqual(org_row.member_count, 0)

    async def test_member_count_matches_actual_rows(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            org.member_count = 99
            a = await self._seed_character(db, project.id, "甲")
            b = await self._seed_character(db, project.id, "乙")
            db.add(OrganizationMember(organization_id=org.id, character_id=a.id, position="弟子"))
            db.add(OrganizationMember(organization_id=org.id, character_id=b.id, position="长老"))
            await db.flush()

            count = await sync_organization_member_count(org, db)
            self.assertEqual(count, 2)
            self.assertEqual(org.member_count, 2)

    async def test_full_project_import_keeps_name_links(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "完整源")
            org_char = await self._seed_character(db, source.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, source.id, org_char)
            member_char = await self._seed_character(db, source.id, "沈青禾")
            await add_organization_members_batch(
                db, org, [{"character_id": member_char.id, "position": "内门弟子"}]
            )
            await db.commit()

            exported = await ImportExportService.export_project(source.id, db)
            members = exported.organization_members
            self.assertEqual(len(members), 1)
            self.assertEqual(members[0].organization_name, "青云宗")
            self.assertEqual(members[0].character_name, "沈青禾")

            imported = await ImportExportService.import_project(
                exported.model_dump(),
                db,
                "user-1",
            )
            self.assertTrue(imported.success, imported.message)

            new_org = (
                await db.execute(
                    select(Organization).join(Character, Organization.character_id == Character.id).where(
                        Organization.project_id == imported.project_id,
                        Character.name == "青云宗",
                    )
                )
            ).scalar_one()
            new_members = (
                await db.execute(
                    select(OrganizationMember).where(OrganizationMember.organization_id == new_org.id)
                )
            ).scalars().all()
            self.assertEqual(len(new_members), 1)
            self.assertEqual(new_org.member_count, 1)

    async def test_restore_skips_existing_and_keeps_imported_characters(self):
        async with await self._session() as db:
            project = await self._seed_project(db)
            org_char = await self._seed_character(db, project.id, "青云宗", is_organization=True)
            org = await self._seed_org(db, project.id, org_char)
            member_char = await self._seed_character(db, project.id, "沈青禾")
            await add_organization_members_batch(
                db, org, [{"character_id": member_char.id, "position": "弟子"}]
            )
            warnings: list[str] = []
            added = await restore_organization_members(
                db,
                org,
                [{"character_name": "沈青禾", "position": "长老"}],
                project_id=project.id,
                warnings=warnings,
            )
            self.assertEqual(added, 0)
            self.assertTrue(any("已属于" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
