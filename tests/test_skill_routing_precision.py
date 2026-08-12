from pathlib import Path

from repo_agent.skills import SkillCatalog, SkillRouter


def test_generic_python_tag_does_not_activate_refactor_skill() -> None:
    skills_root = Path(__file__).resolve().parents[1] / "skills"
    catalog = SkillCatalog((skills_root,))
    catalog.refresh()

    matches = SkillRouter().route(
        "定位 Python 服务中的空指针异常",
        catalog.descriptors(),
        mode="diagnose",
    )

    assert matches == ()
