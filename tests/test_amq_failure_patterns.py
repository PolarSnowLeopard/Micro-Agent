from __future__ import annotations

from scripts.analyze_amq_failure_patterns import (
    build_report,
    classify_adaptation_errors,
    classify_evaluation_result,
    classify_generation_failure,
)


def test_generation_failure_classifier_is_multilabel() -> None:
    labels = classify_generation_failure(
        "[smoke_test] output schema mismatch after adapter "
        "未调用规划中的任何源码能力"
    )

    assert labels == [
        "smoke_fixture_grounding",
        "source_execution_fidelity",
        "schema_runtime_mismatch",
    ]


def test_adaptation_failure_classifier_covers_contract_and_interface() -> None:
    labels = classify_adaptation_errors(
        [
            "模板缺少 tests_ioeb/test_template_contract.py",
            "main_process 显式参数过多",
        ]
    )

    assert labels == [
        "executable_contract_missing",
        "interface_overbroad",
    ]


def test_adaptation_classifier_separates_remote_paths_and_missing_assets() -> None:
    labels = classify_adaptation_errors(
        [
            "main_process 不得要求远程调用者提供容器内路径；"
            "仓库缺少完成真实算法调用所需的模型或数据资产，"
            "不能生成伪 checkpoint"
        ]
    )

    assert labels == [
        "server_path_interface",
        "missing_runtime_assets",
    ]


def test_failure_classifier_preserves_unclassified_signal() -> None:
    assert classify_generation_failure("unexpected opaque failure") == [
        "unclassified"
    ]


def test_generic_plan_failure_is_not_misclassified_as_submission_protocol() -> None:
    assert classify_generation_failure("未能提交有效封装规划：接口质量门禁失败") == [
        "unclassified"
    ]
    assert classify_generation_failure(
        "Agent 在有限步骤内未调用 save_packaging_plan_json"
    ) == ["agent_submission_protocol"]


def test_evaluation_classifier_separates_cross_sample_failure_stages() -> None:
    assert classify_evaluation_result({"d1_build_success": False}) == [
        "availability_build"
    ]
    assert classify_evaluation_result(
        {"d1_build_success": True, "d1_service_health": False}
    ) == ["availability_health"]
    assert classify_evaluation_result(
        {
            "d1_build_success": True,
            "d1_service_health": True,
            "d3_pass": False,
            "d3_driver_status": "completed",
            "d3_total_calls": 0,
            "d3_successful_calls": 0,
        }
    ) == ["tool_not_invoked"]
    assert classify_evaluation_result(
        {
            "d1_build_success": True,
            "d1_service_health": True,
            "d3_pass": False,
            "d3_driver_status": "max_turns",
            "d3_total_calls": 8,
            "d3_successful_calls": 0,
        }
    ) == ["solver_exhaustion", "tool_invocation_failure"]
    assert classify_evaluation_result(
        {
            "d1_build_success": True,
            "d1_service_health": True,
            "d3_pass": False,
            "d3_driver_status": "completed",
            "d3_total_calls": 2,
            "d3_successful_calls": 1,
        }
    ) == ["semantic_result_gap"]


def test_evaluation_report_deduplicates_retried_samples(
    tmp_path,
) -> None:
    first = tmp_path / "first.json"
    first.write_text(
        """
        {"results": [{
          "sample_id": "sample-a",
          "d1_build_success": true,
          "d1_service_health": true,
          "d3_pass": false,
          "d3_total_calls": 1,
          "d3_successful_calls": 0
        }]}
        """,
        encoding="utf-8",
    )
    retry = tmp_path / "retry.json"
    retry.write_text(
        """
        {"results": [{
          "sample_id": "sample-a",
          "d1_build_success": true,
          "d1_service_health": true,
          "d3_pass": true,
          "d3_total_calls": 1,
          "d3_successful_calls": 1
        }]}
        """,
        encoding="utf-8",
    )

    report = build_report([], [], [first, retry])
    evaluation = report["evaluationCrossSection"]

    assert evaluation["sampleCount"] == 1
    assert evaluation["duplicateSampleCounts"] == {"sample-a": 1}
    assert evaluation["failurePatternCounts"] == {"passed": 1}
