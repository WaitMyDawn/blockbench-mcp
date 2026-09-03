"""无视觉质检 + 样例库测试。"""

from __future__ import annotations

from blockbench_mcp import examples, quality, tools


def test_catalog_contains_core_samples() -> None:
    ids = examples.sample_ids()
    assert "redeemer" in ids
    assert "polar_bear" in ids
    assert "bettermodel_demon_knight" in ids
    assert "img2bb_coyote" in ids
    info = examples.sample_info("polar_bear")
    assert info["license_spdx"] == "MIT"
    assert examples.sample_info("redeemer")["license"].startswith("ARR")
    assert examples.sample_info("bettermodel_demon_knight")["license_spdx"] == "MIT"


def test_load_file_sample_polar_bear() -> None:
    p = examples.load("polar_bear")
    assert p.format_id == "bedrock"
    assert p.summary()["counts"]["elements"] == 10
    assert p.validate() == []


def test_load_redeemer_sample() -> None:
    p = examples.load("redeemer")
    assert p.format_id == "geckolib_model"
    assert p.summary()["counts"]["elements"] == 234
    assert p.validate() == []


def test_all_catalog_samples_load_clean() -> None:
    for sample_id in examples.sample_ids():
        p = examples.load(sample_id)
        assert p.validate() == [], f"{sample_id} 存在一致性问题"


def test_quality_sword_template_no_errors() -> None:
    p = examples.load("sword_geckolib")
    report = quality.quality_report(p)
    levels = {i["level"] for i in report["issues"]}
    assert "error" not in levels
    assert report["metrics"]["elements"] == 6


def test_quality_empty_project_error() -> None:
    from blockbench_mcp.document import BlockbenchProject

    report = quality.quality_report(BlockbenchProject(name="empty"))
    codes = {i["code"] for i in report["issues"]}
    assert "empty_project" in codes


def test_tool_validate_quality_and_load_example() -> None:
    result = tools.project_load_example("sword_geckolib")
    assert result["data"]["license_spdx"] is None  # 自研模板无外部许可
    report = tools.validate_quality()
    assert report["data"]["metrics"]["elements"] == 6
