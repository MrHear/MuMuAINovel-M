"""角色包：人物关系 / 职业定义 / 职业关联的可移植导入导出测试。"""
import json
import unittest
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.career import Career, CharacterCareer
from app.models.character import Character
from app.models.project import Project
from app.models.relationship import CharacterRelationship, Organization, OrganizationMember
from app.services.import_export_service import ImportExportService
from app.services.organization_member_service import add_organization_members_batch


def _uid() -> str:
    return str(uuid.uuid4())


class PortableCharacterPackTests(unittest.IsolatedAsyncioTestCase):
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
    ) -> Character:
        character = Character(
            id=_uid(),
            project_id=project_id,
            name=name,
            is_organization=is_organization,
            role_type="supporting",
        )
        db.add(character)
        await db.flush()
        return character

    async def _seed_career(
        self,
        db: AsyncSession,
        project_id: str,
        name: str,
        career_type: str = "main",
        description: str = "剑道",
        stages: str = '[{"level":1,"name":"入门"}]',
    ) -> Career:
        career = Career(
            id=_uid(),
            project_id=project_id,
            name=name,
            type=career_type,
            description=description,
            stages=stages,
            max_stage=10,
            source="manual",
        )
        db.add(career)
        await db.flush()
        return career

    async def _seed_relationship(
        self,
        db: AsyncSession,
        project_id: str,
        source: Character,
        target: Character,
        name: str = "同伴",
    ) -> CharacterRelationship:
        rel = CharacterRelationship(
            id=_uid(),
            project_id=project_id,
            character_from_id=source.id,
            character_to_id=target.id,
            relationship_name=name,
            intimacy_level=60,
            status="active",
            description="同行",
            started_at="初入青玄界",
            source="manual",
        )
        db.add(rel)
        await db.flush()
        return rel

    async def _link_career(
        self,
        db: AsyncSession,
        character: Character,
        career: Career,
        career_type: str,
        stage: int = 3,
        progress: int = 20,
    ) -> CharacterCareer:
        link = CharacterCareer(
            id=_uid(),
            character_id=character.id,
            career_id=career.id,
            career_type=career_type,
            current_stage=stage,
            stage_progress=progress,
        )
        db.add(link)
        if career_type == "main":
            character.main_career_id = career.id
            character.main_career_stage = stage
        else:
            existing = json.loads(character.sub_careers) if character.sub_careers else []
            existing.append({"career_id": career.id, "stage": stage})
            character.sub_careers = json.dumps(existing, ensure_ascii=False)
        await db.flush()
        return link

    async def test_export_import_restores_relationship_by_name(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            heng = await self._seed_character(db, source.id, "衡析")
            shen = await self._seed_character(db, source.id, "沈青禾")
            await self._seed_relationship(db, source.id, heng, shen)
            await db.commit()

            exported = await ImportExportService.export_characters([heng.id, shen.id], db)
            self.assertEqual(exported["version"], "1.2.0")
            self.assertEqual(len(exported["relationships"]), 1)
            self.assertEqual(exported["relationships"][0]["source_name"], "衡析")
            self.assertEqual(exported["relationships"][0]["target_name"], "沈青禾")

            target = await self._seed_project(db, "目标")
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["statistics"]["imported_relationships"], 1)

            rels = (
                await db.execute(
                    select(CharacterRelationship).where(CharacterRelationship.project_id == target.id)
                )
            ).scalars().all()
            self.assertEqual(len(rels), 1)
            names = {
                (await db.execute(select(Character).where(Character.id == rels[0].character_from_id))).scalar_one().name,
                (await db.execute(select(Character).where(Character.id == rels[0].character_to_id))).scalar_one().name,
            }
            self.assertEqual(names, {"衡析", "沈青禾"})
            self.assertEqual(rels[0].character_from_id != heng.id, True)

    async def test_relationship_restores_when_target_already_exists(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            heng = await self._seed_character(db, source.id, "衡析")
            shen = await self._seed_character(db, source.id, "沈青禾")
            await self._seed_relationship(db, source.id, heng, shen)
            await db.commit()

            exported = await ImportExportService.export_characters([heng.id], db)
            self.assertEqual(len(exported["relationships"]), 1)

            target = await self._seed_project(db, "目标")
            await self._seed_character(db, target.id, "沈青禾")
            await db.commit()

            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            rels = (
                await db.execute(
                    select(CharacterRelationship).where(CharacterRelationship.project_id == target.id)
                )
            ).scalars().all()
            self.assertEqual(len(rels), 1)

    async def test_missing_relationship_target_warns_and_keeps_characters(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            heng = await self._seed_character(db, source.id, "衡析")
            shen = await self._seed_character(db, source.id, "沈青禾")
            await self._seed_relationship(db, source.id, heng, shen)
            await db.commit()

            exported = await ImportExportService.export_characters([heng.id], db)
            target = await self._seed_project(db, "空目标")
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["statistics"]["imported"], 1)
            self.assertGreaterEqual(result["statistics"]["skipped_relationships"], 1)
            self.assertTrue(any("沈青禾" in w for w in result["warnings"]))
            rels = (
                await db.execute(
                    select(CharacterRelationship).where(CharacterRelationship.project_id == target.id)
                )
            ).scalars().all()
            self.assertEqual(rels, [])

    async def test_duplicate_relationship_is_skipped(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            heng = await self._seed_character(db, source.id, "衡析")
            shen = await self._seed_character(db, source.id, "沈青禾")
            await self._seed_relationship(db, source.id, heng, shen)
            await db.commit()
            exported = await ImportExportService.export_characters([heng.id, shen.id], db)

            target = await self._seed_project(db, "目标")
            first = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertEqual(first["statistics"]["imported_relationships"], 1)
            second = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertEqual(second["statistics"]["imported_relationships"], 0)
            self.assertGreaterEqual(second["statistics"]["skipped_relationships"], 1)
            rels = (
                await db.execute(
                    select(CharacterRelationship).where(CharacterRelationship.project_id == target.id)
                )
            ).scalars().all()
            self.assertEqual(len(rels), 1)

    async def test_main_career_restored_by_name(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            char = await self._seed_character(db, source.id, "陆沉舟")
            career = await self._seed_career(db, source.id, "剑修", "main")
            await self._link_career(db, char, career, "main", 3, 20)
            await db.commit()

            exported = await ImportExportService.export_characters([char.id], db)
            self.assertTrue(any(item["career_name"] == "剑修" for item in exported["character_careers"]))
            self.assertTrue(any(item["name"] == "剑修" for item in exported["careers"]))

            target = await self._seed_project(db, "目标")
            await self._seed_career(db, target.id, "剑修", "main", description="目标已有剑修")
            await db.commit()
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["statistics"]["imported_character_careers"], 1)

            imported_char = (
                await db.execute(select(Character).where(Character.project_id == target.id, Character.name == "陆沉舟"))
            ).scalar_one()
            target_career = (
                await db.execute(select(Career).where(Career.project_id == target.id, Career.name == "剑修"))
            ).scalar_one()
            self.assertEqual(imported_char.main_career_id, target_career.id)
            self.assertNotEqual(imported_char.main_career_id, career.id)
            links = (
                await db.execute(select(CharacterCareer).where(CharacterCareer.character_id == imported_char.id))
            ).scalars().all()
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].current_stage, 3)

    async def test_sub_career_restored_by_name(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            char = await self._seed_character(db, source.id, "陆沉舟")
            career = await self._seed_career(db, source.id, "丹道", "sub")
            await self._link_career(db, char, career, "sub", 2, 10)
            await db.commit()

            exported = await ImportExportService.export_characters([char.id], db)
            target = await self._seed_project(db, "目标")
            await self._seed_career(db, target.id, "丹道", "sub")
            await db.commit()
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            imported_char = (
                await db.execute(select(Character).where(Character.project_id == target.id, Character.name == "陆沉舟"))
            ).scalar_one()
            subs = json.loads(imported_char.sub_careers or "[]")
            self.assertEqual(len(subs), 1)
            self.assertNotEqual(subs[0]["career_id"], career.id)

    async def test_missing_career_definition_is_created_from_pack(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            char = await self._seed_character(db, source.id, "陆沉舟")
            career = await self._seed_career(db, source.id, "剑修", "main")
            await self._link_career(db, char, career, "main")
            await db.commit()

            exported = await ImportExportService.export_characters([char.id], db)
            target = await self._seed_project(db, "空职业项目")
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            careers = (
                await db.execute(select(Career).where(Career.project_id == target.id))
            ).scalars().all()
            self.assertEqual(len(careers), 1)
            self.assertEqual(careers[0].name, "剑修")
            imported_char = (
                await db.execute(select(Character).where(Character.project_id == target.id, Character.name == "陆沉舟"))
            ).scalar_one()
            self.assertEqual(imported_char.main_career_id, careers[0].id)

    async def test_existing_same_career_is_reused(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            char = await self._seed_character(db, source.id, "陆沉舟")
            career = await self._seed_career(db, source.id, "剑修", "main")
            await self._link_career(db, char, career, "main")
            await db.commit()
            exported = await ImportExportService.export_characters([char.id], db)

            target = await self._seed_project(db, "目标")
            existing = await self._seed_career(db, target.id, "剑修", "main", description="剑道")
            await db.commit()
            await ImportExportService.import_characters(exported, target.id, "user-1", db)
            careers = (
                await db.execute(select(Career).where(Career.project_id == target.id, Career.name == "剑修"))
            ).scalars().all()
            self.assertEqual(len(careers), 1)
            self.assertEqual(careers[0].id, existing.id)

    async def test_same_name_different_career_does_not_overwrite(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "源")
            char = await self._seed_character(db, source.id, "陆沉舟")
            career = await self._seed_career(db, source.id, "剑修", "main", description="源定义")
            await self._link_career(db, char, career, "main")
            await db.commit()
            exported = await ImportExportService.export_characters([char.id], db)

            target = await self._seed_project(db, "目标")
            existing = await self._seed_career(db, target.id, "剑修", "main", description="目标定义", stages='[{"level":1,"name":"不同"}]')
            await db.commit()
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(any("不覆盖" in w or "不一致" in w for w in result["warnings"]))
            refreshed = (
                await db.execute(select(Career).where(Career.id == existing.id))
            ).scalar_one()
            self.assertEqual(refreshed.description, "目标定义")

    def test_validate_accepts_old_character_pack_versions(self):
        for version in ("1.0.0", "1.1.0"):
            result = ImportExportService.validate_characters_import({
                "version": version,
                "export_type": "characters",
                "data": [{"name": "陆沉舟", "main_career_id": "old-uuid"}],
            })
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["version"], version)

    async def test_old_career_uuid_file_does_not_500(self):
        async with await self._session() as db:
            target = await self._seed_project(db, "目标")
            payload = {
                "version": "1.1.0",
                "export_time": "2026-01-01T00:00:00",
                "export_type": "characters",
                "count": 1,
                "data": [
                    {
                        "name": "陆沉舟",
                        "is_organization": False,
                        "main_career_id": _uid(),
                        "main_career_stage": 2,
                        "sub_careers": json.dumps([{"career_id": _uid(), "stage": 1}]),
                    }
                ],
            }
            result = await ImportExportService.import_characters(payload, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            imported = (
                await db.execute(select(Character).where(Character.project_id == target.id, Character.name == "陆沉舟"))
            ).scalar_one()
            self.assertIsNone(imported.main_career_id)
            self.assertTrue(any("职业" in w for w in result["warnings"]))

    async def test_full_character_pack_roundtrip(self):
        async with await self._session() as db:
            source = await self._seed_project(db, "完整源")
            org_char = await self._seed_character(db, source.id, "青云宗", is_organization=True)
            org = Organization(
                id=_uid(),
                character_id=org_char.id,
                project_id=source.id,
                member_count=0,
            )
            db.add(org)
            await db.flush()
            heng = await self._seed_character(db, source.id, "衡析")
            shen = await self._seed_character(db, source.id, "沈青禾")
            await add_organization_members_batch(db, org, [{"character_id": shen.id, "position": "内门弟子"}])
            await self._seed_relationship(db, source.id, heng, shen, "同伴")
            main = await self._seed_career(db, source.id, "剑修", "main")
            sub = await self._seed_career(db, source.id, "丹道", "sub")
            await self._link_career(db, heng, main, "main", 3, 20)
            await self._link_career(db, heng, sub, "sub", 2, 10)
            await db.commit()

            exported = await ImportExportService.export_characters(
                [org_char.id, heng.id, shen.id],
                db,
            )
            self.assertEqual(exported["version"], "1.2.0")
            self.assertGreaterEqual(len(exported["relationships"]), 1)
            self.assertGreaterEqual(len(exported["careers"]), 2)
            self.assertGreaterEqual(len(exported["character_careers"]), 2)

            target = await self._seed_project(db, "空项目")
            result = await ImportExportService.import_characters(exported, target.id, "user-1", db)
            self.assertTrue(result["success"], result)
            self.assertEqual(result["statistics"]["imported_relationships"], 1)
            self.assertEqual(result["statistics"]["imported_character_careers"], 2)
            self.assertGreaterEqual(result["statistics"]["imported_organization_members"], 1)

            target_chars = (
                await db.execute(select(Character).where(Character.project_id == target.id))
            ).scalars().all()
            self.assertEqual({c.name for c in target_chars}, {"青云宗", "衡析", "沈青禾"})
            rels = (
                await db.execute(
                    select(CharacterRelationship).where(CharacterRelationship.project_id == target.id)
                )
            ).scalars().all()
            self.assertEqual(len(rels), 1)
            careers = (
                await db.execute(select(Career).where(Career.project_id == target.id))
            ).scalars().all()
            self.assertEqual({(c.name, c.type) for c in careers}, {("剑修", "main"), ("丹道", "sub")})
            heng_t = next(c for c in target_chars if c.name == "衡析")
            links = (
                await db.execute(select(CharacterCareer).where(CharacterCareer.character_id == heng_t.id))
            ).scalars().all()
            self.assertEqual(len(links), 2)
            members = (
                await db.execute(select(OrganizationMember).join(Organization).where(Organization.project_id == target.id))
            ).scalars().all()
            self.assertEqual(len(members), 1)

            reexport = await ImportExportService.export_characters([c.id for c in target_chars], db)
            self.assertEqual(len(reexport["relationships"]), 1)
            self.assertEqual({item["career_name"] for item in reexport["character_careers"]}, {"剑修", "丹道"})
            self.assertEqual(reexport["data"][next(i for i, item in enumerate(reexport["data"]) if item["name"] == "青云宗")].get("organization_members_data")[0]["character_name"], "沈青禾")


if __name__ == "__main__":
    unittest.main()
