"""Load the immutable Artifact of a published meta-app from IOEB."""

from __future__ import annotations

from typing import Any

import httpx

from micro_agent.core.config import config


class PublishedMetaAppError(ValueError):
    """The platform record cannot be executed as a published meta-app."""


async def load_published_artifact(
    meta_app_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    app_id = str(meta_app_id or "").strip()
    if not app_id:
        raise PublishedMetaAppError("meta_app_id 不能为空")

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=config.platform.request_timeout)
    url = f"{config.platform.ioeb_api_base_url.rstrip('/')}/services/{app_id}"
    try:
        response = await http.get(url)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PublishedMetaAppError(f"读取元应用配置失败 [{app_id}]: {exc}") from exc
    finally:
        if owns_client:
            await http.aclose()

    service = payload.get("service") if isinstance(payload, dict) else None
    if not isinstance(service, dict) or service.get("type") != "meta":
        raise PublishedMetaAppError(f"元应用不存在或类型错误 [{app_id}]")
    apis = service.get("apiList") or []
    api = apis[0] if len(apis) == 1 and isinstance(apis[0], dict) else None
    artifact = api.get("metaAppArtifact") if api else None
    if not isinstance(artifact, dict):
        raise PublishedMetaAppError(f"元应用未关联 Artifact [{app_id}]")
    _validate_artifact(artifact, app_id)
    return artifact


def _validate_artifact(artifact: dict[str, Any], app_id: str) -> None:
    if artifact.get("schemaVersion") != "meta_app_artifact.v1":
        raise PublishedMetaAppError(f"Artifact Schema 不受支持 [{app_id}]")
    if not isinstance(artifact.get("artifactId"), str) or not artifact["artifactId"]:
        raise PublishedMetaAppError(f"Artifact 缺少 artifactId [{app_id}]")
    for field in ("app", "taskContract", "runtime"):
        if not isinstance(artifact.get(field), dict):
            raise PublishedMetaAppError(f"Artifact 缺少 {field} [{app_id}]")
    bindings = artifact["runtime"].get("serviceBindings")
    if not isinstance(bindings, list):
        raise PublishedMetaAppError(f"Artifact 缺少 serviceBindings [{app_id}]")
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("isFake"), bool):
            raise PublishedMetaAppError(f"Artifact serviceBinding 必须显式声明 isFake [{app_id}]")
    if not isinstance(artifact.get("goldenPaths"), list):
        raise PublishedMetaAppError(f"Artifact 缺少 goldenPaths [{app_id}]")
