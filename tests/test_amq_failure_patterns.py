from __future__ import annotations

from scripts.analyze_amq_failure_patterns import (
    classify_adaptation_errors,
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


def test_failure_classifier_preserves_unclassified_signal() -> None:
    assert classify_generation_failure("unexpected opaque failure") == [
        "unclassified"
    ]
