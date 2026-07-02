"""跨端契约锚点：仿真构建产出的黄金 MetaAppArtifact。

真源是 Micro-Agent 的确定性编译器（``build_meta_app_artifact`` 作用于
``test_build_bundle_contract._trace()``）。``golden_meta_app_artifact.json``
由 MA 生成，ioeb_backend 与 ioeb 各持一份相同副本作为消费方。

勿手改 JSON 或下列常量：任一端改动构建产物契约都会让三端的
hash/id 断言失败，从而暴露漂移。
"""

import json
from pathlib import Path

GOLDEN_ARTIFACT_ID = "app-37e3436d473b4479"
GOLDEN_ARTIFACT_HASH = "088f717621f73fbaf0d4a3accc661776633c8f5ef3042e2d48ec2e913607ad15"

_FIXTURE = Path(__file__).parent / "golden_meta_app_artifact.json"


def load_golden_artifact() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))
