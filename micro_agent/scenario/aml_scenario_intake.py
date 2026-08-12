"""算法模型想定式开发 — 对话填表（自然语言 → formDraft）。

按缺口一次一问引导澄清；开场后追问最多五轮；字典 code 必须落在 snapshot 内。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from micro_agent.core.config import config
from micro_agent.core.llm import LLM
from micro_agent.core.memory.persistent import FileMemory
from micro_agent.core.schema import Message, Role

_MAX_FOLLOWUPS = 5

_VALID_CATEGORIES = {
    "classification",
    "detection",
    "regression",
    "clustering",
    "generation",
    "recommendation",
}

_CATEGORY_PARAM_KEYS = {
    "classification": {"inputTypes", "outputTypes", "labels", "multiLabel", "constraints"},
    "detection": {"inputTypes", "targetTypes", "outputFormats", "realtime", "constraints"},
    "regression": {
        "inputTypes",
        "predictionTarget",
        "timeGranularity",
        "metrics",
        "constraints",
    },
    "clustering": {
        "inputTypes",
        "clusterCount",
        "methods",
        "outputFormats",
        "constraints",
    },
    "generation": {
        "inputTypes",
        "targetTypes",
        "generateCount",
        "qualityPreference",
        "constraints",
    },
    "recommendation": {
        "inputTypes",
        "recommendTarget",
        "strategies",
        "topK",
        "constraints",
    },
}

_SLOT_QUESTIONS = {
    "goal": "先说说您最想用算法解决什么业务问题？",
    # 禁止直接问「分类/回归」等术语；用业务结果形态来推断类别
    "algorithm_category": (
        "希望算法最终帮您得到哪种结果？"
        "①给每笔业务打个结论或标签；②在数据/图片里找出异常或目标位置；"
        "③预测一个数值（如金额、数量、评分）；④把相似的自动分成几组；"
        "⑤自动生成文字/图片等内容；⑥给出推荐选项。"
    ),
    "input": "日常业务里，算法主要会用到哪些已有资料？（例如表格、流水、日志、图片、文本描述等，用您熟悉的说法即可）",
    "output": "业务人员最终希望在页面上看到什么？怎么才算「看得懂、用得上」？",
    "scenario_use": "这个能力主要用在哪个业务环节？（例如审核前筛、事中监测、事后复查等）",
}

_CATEGORY_PARAM_QUESTIONS = {
    "classification": "结果希望分成哪几类？请用业务说法列出（例如：正常、低风险、高风险）",
    "detection": "需要重点找出来的异常或目标是什么？（例如：伪造单据、违禁物品、异常区域）",
    "regression": "您最想预测的那个数值具体是什么？（例如：未来7天交易额、违约概率分）",
    "clustering": "您大概希望自动分成几组，或按什么业务含义分组？（说不清也可以说「先自动分」）",
    "generation": "希望自动生成什么内容？（例如：审核意见草稿、摘要、示意图说明）",
    "recommendation": "希望推荐的是什么？每次大概推荐几条？（例如：相似案例、处置建议）",
}

_CATEGORY_PARAM_HINTS = {
    "classification": "正常、低风险、高风险",
    "detection": "可疑交易片段、伪造印章区域",
    "regression": "未来7天可疑交易金额",
    "clustering": "先自动分成3到5组",
    "generation": "一段简短的风险说明文字",
    "recommendation": "每次推荐3条处置建议",
}

_INTAKE_SYSTEM = """你是「算法模型想定式开发」引导式对话助手。面向不懂算法术语的业务人员，用口语一次只问一个问题，并增量完善表单。

目标：
1) 先弄清业务目标，并据此**自行判断** algorithm_category（不要让用户选专业类别名）；
2) 类别明确后，再问输入资料、期望呈现的结果、使用环节；
3) 最后补该类算法的关键业务参数（如分类标签、预测对象等）；
4) 推断并回填 industry/scenario/technology（字典 code）、category_params；
5) 完善 free_narrative（完整中文段落，含功能/输入/输出/场景）。

algorithm_category 仅可为：classification|detection|regression|clustering|generation|recommendation
业务话术与类别对应（只写进 formDraft，不要把英文类别念给用户听）：
- classification：打标签/下结论/是否通过/风险等级
- detection：找出、定位、圈出异常或目标
- regression：预测数值、评分、金额、数量、概率分
- clustering：自动分组、分群、归类相似对象
- generation：自动写文案/报告/图片等内容
- recommendation：推荐选项、相似案例、处置建议

规则（必须遵守）：
- 只输出**单行 JSON**，不要 markdown、不要解释。
- 每轮只问**一个**最关键缺口；话术短、口语；可用 hint 给一句话示例答法。
- **禁止**直接问「您要分类还是回归/检测/聚类」等术语；用「希望算法最后帮您做成什么样」这类业务问题来推断类别。
- 追问顺序必须是：业务目标 →（先用业务友好问题确认结果形态，再写入 algorithm_category）→ 输入资料 → 输出呈现 → 使用环节 → 该类别专属参数。
- **关键**：用户只说了业务目标（如「识别可疑交易」）还不够，**不得立刻问输入数据**；下一问必须先问「希望算法最终帮您得到哪种结果」（①打标签/结论 ②找异常 ③预测数值 ④分组 ⑤生成 ⑥推荐），等用户用业务话确认后，才能问输入。
- 问结果形态时，话术里写「算法」，不要写「系统」。
- 能从用户话里推断的配置尽量填进 formDraft；用户刚答过的点禁止原话重问。
- 仅在用户已用结果形态确认后，才写入 algorithm_category；可用一句大白话复述确认，不要暴露专业术语清单。
- 在类别确认前，formDraft 可先完善 free_narrative/行业场景，但不要把追问跳到输入/字段细节。
- industry/scenario/technology/algorithm_category 的 code **必须**来自「可选字典」；无法匹配则省略。
- category_params 只保留与 algorithm_category 相关的键。
- 分类任务：用户一旦给出标签（如「正常、低风险、高风险」），必须立刻写入 category_params.labels 为**字符串数组**。
- 若用户已有 partial_form，在其基础上增量修订，不要无故清空。
- 关键信息已够：status=updated，给出完整 free_narrative，并提示可检查右侧叙述与上方配置后生成。
- 若系统提示「已达追问上限」：禁止再追问，必须 status=updated，基于已有信息尽力补全叙述与配置。

输出格式（二选一）：
更新：{"status":"updated","text":"确认说明","hint":"可选","formDraft":{"model_name":"可选","free_narrative":"…","industry":"code","scenario":"code","technology":"code","algorithm_category":"classification","category_params":{}},"changedFields":["free_narrative","industry"]}
追问：{"status":"question","text":"单个追问","hint":"可选回答建议","formDraft":{...已推断字段...},"changedFields":[...]}
"""


def _infer_category_from_text(text: str) -> str | None:
    """从业务表述推断算法类别；拿不准则返回 None。"""
    t = (text or "").strip()
    if not t:
        return None

    digit_map = {
        "1": "classification",
        "2": "detection",
        "3": "regression",
        "4": "clustering",
        "5": "generation",
        "6": "recommendation",
        "①": "classification",
        "②": "detection",
        "③": "regression",
        "④": "clustering",
        "⑤": "generation",
        "⑥": "recommendation",
    }
    if t in digit_map:
        return digit_map[t]
    # 对话历史里单独一行的 1-6 / ①-⑥（取最后一次）
    for line in reversed(t.splitlines()):
        key = line.strip()
        if key in digit_map:
            return digit_map[key]

    # 选项编号/口语结果形态（优先）
    choice_map = [
        (r"(①|1\s*[\.、:：)]|打个结论|打个标签|打个风险|下个结论|给.*标签|是否通过|风险等级|风险结论)", "classification"),
        (r"(②|2\s*[\.、:：)]|找出异常|定位|圈出|标出位置)", "detection"),
        (r"(③|3\s*[\.、:：)]|预测.*数|预估|金额|数量|评分|概率)", "regression"),
        (r"(④|4\s*[\.、:：)]|分成几组|自动分组|分群|归类相似)", "clustering"),
        (r"(⑤|5\s*[\.、:：)]|自动生成|写一封|生成文字|生成图片|生成报告)", "generation"),
        (r"(⑥|6\s*[\.、:：)]|推荐|相似案例|处置建议)", "recommendation"),
    ]
    for pattern, cat in choice_map:
        if re.search(pattern, t):
            return cat

    # 关键词兜底（避免过短闲聊误判）
    if len(t) < 4:
        return None
    rules = [
        ("recommendation", ("推荐", "相似商品", "猜你喜欢", "处置建议")),
        ("generation", ("生成文案", "生成报告", "自动撰写", "生成图片", "写摘要")),
        ("clustering", ("分群", "聚类", "分成几组", "客群划分")),
        ("detection", ("检测", "定位异常", "找出位置", "目标框", "违禁")),
        ("regression", ("预测", "预估", "回归", "数值", "评分预测")),
        ("classification", ("分类", "打标签", "是否可疑", "风险等级", "风险结论", "正常还是", "判别", "打个风险")),
    ]
    for cat, keys in rules:
        if any(k in t for k in keys):
            return cat
    return None


def _has_category_confirm_signal(text: str) -> bool:
    """用户是否已用「结果形态」确认过类别（仅有业务目标不够）。"""
    t = (text or "").strip()
    if not t:
        return False
    # 单独回复 1-6 / ①-⑥（整句或其中一行）也视为已选结果形态
    if re.fullmatch(r"[1-6]", t) or re.fullmatch(r"[①②③④⑤⑥]", t):
        return True
    if re.search(r"(?m)^[1-6]$", t) or re.search(r"(?m)^[①②③④⑤⑥]$", t):
        return True
    # 明确选择①-⑥或同义业务结果描述
    if re.search(r"[①②③④⑤⑥]", t):
        return True
    if re.search(r"[1-6]\s*[\.、:：)]", t):
        return True
    markers = (
        "打个结论",
        "打个标签",
        "打个风险",
        "下个结论",
        "风险等级",
        "风险结论",
        "给每笔",
        "打标签",
        "是否通过",
        "找出异常",
        "圈出",
        "标出位置",
        "定位异常",
        "预测一个数值",
        "预测数值",
        "预估金额",
        "自动分组",
        "分成几组",
        "分群",
        "自动生成",
        "生成文字",
        "生成报告",
        "生成图片",
        "推荐选项",
        "推荐几条",
        "相似案例",
        "处置建议",
    )
    if any(m in t for m in markers):
        return True
    # 对「听起来是要…对吗」的短确认，需同时能推断类别
    if t in ("对", "对的", "是的", "是", "没错", "可以", "就这个", "按这个"):
        return False
    return False


def _reply_asks_about_input(reply: str) -> bool:
    r = reply or ""
    return any(
        k in r
        for k in ("输入", "依据哪些数据", "哪些数据", "什么数据", "数据字段", "特征", "流水字段")
    )


def _slot_question(slot: str, category: str | None = None) -> str:
    if slot == "category_key_params":
        cat = str(category or "")
        return _CATEGORY_PARAM_QUESTIONS.get(cat) or "还有哪些业务上必须定下来的关键规则或参数？"
    return _SLOT_QUESTIONS.get(slot, "还能补充一下关键细节吗？")


def _slot_hint(slot: str, category: str | None = None) -> str:
    if slot == "category_key_params":
        return _CATEGORY_PARAM_HINTS.get(str(category or ""), "")
    return {
        "goal": "识别跨境支付中的可疑交易，并给出可解释结论",
        "algorithm_category": "给每笔交易打个风险结论/标签",
        "input": "业务系统里的表格或交易流水",
        "output": "页面上显示风险结论，并附一句简短原因",
        "scenario_use": "跨境支付事中监测、审核人员复核",
    }.get(slot, "")


def _parse_json_blob(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _parse_json_form(value: str | None) -> Any:
    if not value or not str(value).strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _codes_from_options(options: Any) -> set[str]:
    codes: set[str] = set()
    if not isinstance(options, list):
        return codes
    for item in options:
        if isinstance(item, dict) and item.get("code") is not None:
            codes.add(str(item["code"]))
        elif isinstance(item, str):
            codes.add(item)
    return codes


def _format_dict_options(label: str, options: Any) -> str:
    if not isinstance(options, list) or not options:
        return f"- {label}: （无）"
    parts = []
    for item in options[:40]:
        if isinstance(item, dict):
            parts.append(f"{item.get('code')}={item.get('text') or item.get('code')}")
        else:
            parts.append(str(item))
    return f"- {label}: " + ", ".join(parts)


def _build_snapshot_section(snapshot: dict[str, Any] | None) -> str:
    snap = snapshot if isinstance(snapshot, dict) else {}
    lines = [
        "\n可选字典（code 必须从此选择）：",
        _format_dict_options("industry", snap.get("industry")),
        _format_dict_options("scenario", snap.get("scenario")),
        _format_dict_options("technology", snap.get("technology")),
        _format_dict_options("algorithm_category", snap.get("algorithm_category")),
    ]
    for key in (
        "algo_input_type",
        "algo_constraint",
        "algo_classification_output_type",
        "algo_detection_target_type",
        "algo_detection_output_format",
        "algo_regression_time_granularity",
        "algo_regression_metric",
        "algo_clustering_method",
        "algo_clustering_output_format",
        "algo_generation_target_type",
        "algo_generation_quality",
        "algo_recommendation_strategy",
    ):
        if snap.get(key):
            lines.append(_format_dict_options(key, snap.get(key)))
    return "\n".join(lines)


def _merged_view(partial: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    merged = dict(partial or {})
    for key, value in (draft or {}).items():
        if key == "category_params" and isinstance(value, dict):
            base = merged.get("category_params") if isinstance(merged.get("category_params"), dict) else {}
            merged["category_params"] = {**base, **value}
        elif value is not None and value != "":
            merged[key] = value
    return merged


def _narrative_has_signals(text: str) -> dict[str, bool]:
    t = text or ""
    return {
        "goal": len(t.strip()) >= 8
        or any(k in t for k in ("识别", "预测", "检测", "分类", "推荐", "生成", "解决", "风控", "审核")),
        "input": any(
            k in t
            for k in ("输入", "接收", "数据", "特征", "URL", "表格", "流水", "结构化", "文本", "图片", "日志")
        ),
        "output": any(
            k in t for k in ("输出", "返回", "结果", "标签", "评分", "预测", "置信度", "原因")
        ),
        "scenario_use": any(
            k in t for k in ("场景", "用于", "面向", "业务", "环节", "监测", "风控", "跨境", "支付")
        ),
    }


def _user_corpus_from_memory(memory: FileMemory) -> str:
    parts: list[str] = []
    for item in memory.to_list():
        if (item.get("role") or "") == "user":
            content = (item.get("content") or "").strip()
            if content:
                parts.append(content)
    return "\n".join(parts)


def _parse_label_list(value: Any) -> list[str]:
    """接受数组或「正常、低风险、高风险」类字符串。"""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()][:20]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # 已是 JSON 数组字符串
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()][:20]
            except json.JSONDecodeError:
                pass
        parts = re.split(r"[,，、/;；|]+", text)
        return [p.strip() for p in parts if p.strip()][:20]
    return []


def _looks_like_label_answer(text: str) -> list[str] | None:
    """识别短答标签列表，避免用户已答仍被判定缺 labels。"""
    t = (text or "").strip()
    if not t or len(t) > 100:
        return None
    if any(x in t for x in ("。", "？", "?", "！", "!", "因为", "希望", "需要", "请问")):
        return None
    # 排除明显的长叙述句
    if len(t) > 40 and ("的" in t or "是" in t) and ("、" not in t and "," not in t and "，" not in t):
        return None
    labels = _parse_label_list(t)
    if len(labels) < 2:
        return None
    # 每项不宜过长（更像标签而非句子）
    if any(len(x) > 24 for x in labels):
        return None
    return labels


def _enrich_draft_from_user_text(
    partial: dict[str, Any],
    draft: dict[str, Any],
    user_text: str,
    user_corpus: str = "",
) -> dict[str, Any]:
    """用用户本轮原话修补漏填：仅在结果形态确认后写入算法类别；并修补分类 labels。"""
    view = _merged_view(partial, draft)
    out = dict(draft or {})
    corpus = (user_corpus or user_text or "").strip()

    # 仅当用户已用业务结果形态确认时，才写入/保留类别
    if _has_category_confirm_signal(user_text) or _has_category_confirm_signal(corpus):
        if not view.get("algorithm_category"):
            inferred = _infer_category_from_text(user_text) or _infer_category_from_text(corpus)
            if inferred:
                out["algorithm_category"] = inferred
    else:
        # 类别未确认：去掉模型过早写入的 algorithm_category，避免跳问输入
        if "algorithm_category" in out:
            out.pop("algorithm_category", None)

    cat = str(out.get("algorithm_category") or view.get("algorithm_category") or "")
    if not (_has_category_confirm_signal(user_text) or _has_category_confirm_signal(corpus)):
        cat = str(out.get("algorithm_category") or "")
    params = dict(view.get("category_params") or {}) if isinstance(view.get("category_params"), dict) else {}
    if cat == "classification":
        existing = _parse_label_list(params.get("labels"))
        if len(existing) < 2:
            guessed = _looks_like_label_answer(user_text)
            if guessed:
                next_params = (
                    dict(out.get("category_params") or {})
                    if isinstance(out.get("category_params"), dict)
                    else {}
                )
                base = dict(params)
                base.update(next_params)
                base["labels"] = guessed
                out["category_params"] = base
    return out


def _assess_slots(
    partial: dict[str, Any],
    draft: dict[str, Any],
    user_corpus: str = "",
) -> list[str]:
    """缺口顺序：目标 → 类别(业务确认) → 输入 → 输出 → 场景 → 类别专属参数。"""
    view = _merged_view(partial, draft)
    user_sig = _narrative_has_signals(user_corpus or "")
    cat_confirmed = _has_category_confirm_signal(user_corpus or "")
    missing: list[str] = []
    if not user_sig["goal"]:
        missing.append("goal")

    # 未用业务结果形态确认前，一律先问类别友好问题，且不进入输入
    effective_cat = ""
    if cat_confirmed:
        effective_cat = str(view.get("algorithm_category") or "") or (
            _infer_category_from_text(user_corpus or "") or ""
        )
        if not effective_cat:
            missing.append("algorithm_category")
    else:
        missing.append("algorithm_category")

    if cat_confirmed and effective_cat:
        if not user_sig["input"]:
            missing.append("input")
        if not user_sig["output"]:
            missing.append("output")
        if not user_sig["scenario_use"]:
            missing.append("scenario_use")
        params = view.get("category_params") if isinstance(view.get("category_params"), dict) else {}
        cat = effective_cat
        if cat == "classification":
            labels = _parse_label_list(params.get("labels"))
            if len(labels) < 2:
                missing.append("category_key_params")
        elif cat == "detection":
            targets = params.get("targetTypes")
            if not (isinstance(targets, list) and targets) and not str(
                params.get("constraints") or ""
            ):
                if not any(k in (user_corpus or "") for k in ("找出", "异常", "检测", "定位", "违禁")):
                    missing.append("category_key_params")
        elif cat == "regression" and not str(params.get("predictionTarget") or "").strip():
            missing.append("category_key_params")
        elif cat == "recommendation" and not str(params.get("recommendTarget") or "").strip():
            missing.append("category_key_params")
        elif cat == "generation":
            targets = params.get("targetTypes")
            if not (isinstance(targets, list) and targets):
                if not any(k in (user_corpus or "") for k in ("生成", "文案", "报告", "摘要")):
                    missing.append("category_key_params")
        elif cat == "clustering":
            if params.get("clusterCount") in (None, ""):
                if not any(k in (user_corpus or "") for k in ("分组", "分群", "几组", "类群")):
                    missing.append("category_key_params")
    return missing


def _sanitize_category_params(
    category: str,
    params: Any,
    snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    allowed = _CATEGORY_PARAM_KEYS.get(category) or set()
    snap = snapshot if isinstance(snapshot, dict) else {}
    out: dict[str, Any] = {}

    def filter_codes(values: Any, option_key: str) -> list[str]:
        codes = _codes_from_options(snap.get(option_key))
        if not isinstance(values, list):
            return []
        result = []
        for v in values:
            s = str(v)
            if not codes or s in codes:
                result.append(s)
        return result

    for key, value in params.items():
        if key not in allowed:
            continue
        if key in ("multiLabel", "realtime"):
            out[key] = bool(value)
        elif key in ("clusterCount", "generateCount", "topK"):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif key == "labels":
            parsed_labels = _parse_label_list(value)
            if parsed_labels:
                out[key] = parsed_labels
        elif key in ("predictionTarget", "recommendTarget"):
            text = str(value or "").strip()
            if text:
                out[key] = text[:200]
        elif key == "inputTypes":
            filtered = filter_codes(value, "algo_input_type")
            if filtered:
                out[key] = filtered
        elif key == "constraints":
            filtered = filter_codes(value, "algo_constraint")
            extras = []
            if isinstance(value, list):
                for v in value:
                    s = str(v)
                    if s.startswith("custom:"):
                        extras.append(s[:200])
            merged = filtered + extras
            if merged:
                out[key] = merged
        elif key == "outputTypes":
            filtered = filter_codes(value, "algo_classification_output_type")
            if filtered:
                out[key] = filtered
        elif key == "targetTypes":
            opt = (
                "algo_detection_target_type"
                if category == "detection"
                else "algo_generation_target_type"
            )
            filtered = filter_codes(value, opt)
            if filtered:
                out[key] = filtered
        elif key == "outputFormats":
            opt = (
                "algo_detection_output_format"
                if category == "detection"
                else "algo_clustering_output_format"
            )
            filtered = filter_codes(value, opt)
            if filtered:
                out[key] = filtered
        elif key == "timeGranularity":
            codes = _codes_from_options(snap.get("algo_regression_time_granularity"))
            s = str(value)
            if not codes or s in codes:
                out[key] = s
        elif key == "metrics":
            filtered = filter_codes(value, "algo_regression_metric")
            if filtered:
                out[key] = filtered
        elif key == "methods":
            filtered = filter_codes(value, "algo_clustering_method")
            if filtered:
                out[key] = filtered
        elif key == "qualityPreference":
            filtered = filter_codes(value, "algo_generation_quality")
            if filtered:
                out[key] = filtered
        elif key == "strategies":
            filtered = filter_codes(value, "algo_recommendation_strategy")
            if filtered:
                out[key] = filtered
    return out


def _sanitize_form_draft(
    draft: Any,
    snapshot: dict[str, Any] | None,
    partial: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(draft, dict):
        return {}, []
    snap = snapshot if isinstance(snapshot, dict) else {}
    industry_codes = _codes_from_options(snap.get("industry"))
    scenario_codes = _codes_from_options(snap.get("scenario"))
    technology_codes = _codes_from_options(snap.get("technology"))
    category_codes = _codes_from_options(snap.get("algorithm_category")) or set(
        _VALID_CATEGORIES
    )

    out: dict[str, Any] = {}
    changed: list[str] = []

    narrative = draft.get("free_narrative")
    if isinstance(narrative, str) and narrative.strip():
        out["free_narrative"] = narrative.strip()[:4000]
        changed.append("free_narrative")

    model_name = draft.get("model_name")
    if isinstance(model_name, str) and model_name.strip():
        out["model_name"] = model_name.strip()[:100]
        changed.append("model_name")

    for field, codes in (
        ("industry", industry_codes),
        ("scenario", scenario_codes),
        ("technology", technology_codes),
    ):
        val = draft.get(field)
        if val is None or val == "":
            continue
        code = str(val)
        if not codes or code in codes:
            out[field] = code
            changed.append(field)

    category = draft.get("algorithm_category")
    if category is not None and str(category).strip():
        cat = str(category).strip()
        if cat in category_codes and cat in _VALID_CATEGORIES:
            out["algorithm_category"] = cat
            changed.append("algorithm_category")

    cat_for_params = out.get("algorithm_category") or (
        (partial or {}).get("algorithm_category") if isinstance(partial, dict) else None
    )
    if cat_for_params and isinstance(draft.get("category_params"), dict):
        cleaned = _sanitize_category_params(
            str(cat_for_params), draft.get("category_params"), snap
        )
        if cleaned:
            out["category_params"] = cleaned
            changed.append("category_params")

    return out, changed


def _ensure_narrative_from_partial(partial: dict[str, Any], draft: dict[str, Any], user_text: str) -> dict[str, Any]:
    """追问达上限时，若仍无叙述则拼一份可用草稿。"""
    view = _merged_view(partial, draft)
    if str(view.get("free_narrative") or "").strip():
        if "free_narrative" not in draft and view.get("free_narrative"):
            draft = {**draft, "free_narrative": view["free_narrative"]}
        return draft
    bits = [user_text.strip()] if user_text.strip() else []
    if view.get("algorithm_category"):
        bits.append(f"算法类别倾向为{view['algorithm_category']}。")
    narrative = (
        "请面向当前业务场景生成算法模型。"
        + "".join(bits)
        + "需明确输入与输出形式，结果应便于业务人员理解，并尽量满足平台提交与后续复用要求。"
    )
    draft = {**draft, "free_narrative": narrative[:4000]}
    return draft


def _fallback_without_llm(
    *,
    sid: str,
    text: str,
    partial: dict[str, Any],
    snapshot: dict[str, Any],
    at_limit: bool,
    exc: Exception,
    user_corpus: str = "",
) -> dict[str, Any]:
    """大模型超时/不可用时的规则降级，保证对话可继续。"""
    draft_raw = _enrich_draft_from_user_text(partial, {}, text, user_corpus)
    form_draft, changed = _sanitize_form_draft(draft_raw, snapshot, partial)
    corpus = (user_corpus or text or "").strip()
    if not _has_category_confirm_signal(corpus):
        form_draft.pop("algorithm_category", None)
        changed = [k for k in changed if k != "algorithm_category"]
    # 尽量把用户短答并进叙述，便于右侧可见
    if text and "free_narrative" not in form_draft:
        base_narr = str((partial or {}).get("free_narrative") or "").strip()
        if base_narr:
            form_draft["free_narrative"] = f"{base_narr}\n补充：{text}"[:4000]
            if "free_narrative" not in changed:
                changed.append("free_narrative")
        elif at_limit:
            form_draft = _ensure_narrative_from_partial(partial, form_draft, text)
            changed = list(dict.fromkeys([*changed, *form_draft.keys()]))

    missing = _assess_slots(partial, form_draft, corpus)
    view_cat = str((_merged_view(partial, form_draft).get("algorithm_category") or ""))
    err = str(exc or "")
    is_timeout = "timeout" in err.lower() or "timed out" in err.lower()

    if at_limit or not missing:
        form_draft = _ensure_narrative_from_partial(partial, form_draft, text)
        return {
            "status": "updated",
            "text": (
                "智能助手暂时繁忙，已先根据您刚才的回答保存了信息。"
                "请查看右侧叙述与上方配置，确认后可继续生成；也可稍后再试对话补充。"
            ),
            "session_id": sid,
            "formDraft": form_draft,
            "changedFields": list(dict.fromkeys([*changed, *form_draft.keys()])),
            "degraded": True,
            "degradedReason": "timeout" if is_timeout else "llm_error",
        }

    slot = missing[0]
    return {
        "status": "question",
        "text": (
            "智能助手暂时繁忙，我先记下了您这句回答。"
            + _slot_question(slot, view_cat)
        ),
        "hint": _slot_hint(slot, view_cat),
        "session_id": sid,
        "formDraft": form_draft,
        "changedFields": changed,
        "degraded": True,
        "degradedReason": "timeout" if is_timeout else "llm_error",
    }


async def run_aml_scenario_intake_turn(
    *,
    message: str,
    domain: str = "generic",
    session_id: str | None = None,
    partial_form: str | None = None,
    dictionary_snapshot: str | None = None,
    followup_count: int = 0,
) -> dict[str, Any]:
    """处理一轮用户输入，返回 updated / question + formDraft。"""
    text = (message or "").strip()
    try:
        followup_count = max(0, int(followup_count))
    except (TypeError, ValueError):
        followup_count = 0

    if not text:
        return {
            "status": "question",
            "text": "请先用自然语言描述您想开发的算法模型（目标、输入输出、使用场景）。",
            "session_id": session_id or "",
            "formDraft": {},
            "changedFields": [],
        }

    sid = session_id or uuid.uuid4().hex[:12]
    memory = FileMemory(Path(config.workspace) / config.memory.storage_dir)
    await memory.load(sid)
    memory.add(Message(role=Role.USER, content=text))

    snapshot = _parse_json_form(dictionary_snapshot)
    if not isinstance(snapshot, dict):
        snapshot = {}
    partial = _parse_json_form(partial_form)
    if not isinstance(partial, dict):
        partial = {}

    at_limit = followup_count >= _MAX_FOLLOWUPS
    limit_note = (
        f"\n\n【系统】当前 followup_count={followup_count}，已达追问上限 {_MAX_FOLLOWUPS}。"
        "禁止再追问，必须 status=updated，基于已有信息尽力补全 free_narrative 与配置。"
        if at_limit
        else f"\n\n【系统】当前 followup_count={followup_count}（开场后最多再追问 {_MAX_FOLLOWUPS} 轮）。"
        f"剩余追问额度：{max(0, _MAX_FOLLOWUPS - followup_count)}。"
    )

    # 基于用户原话预估缺口，引导模型按顺序追问（避免直接跳到分类标签）
    pre_missing = _assess_slots(partial, {}, _user_corpus_from_memory(memory))
    if at_limit:
        gap_note = ""
    elif pre_missing:
        gap_note = (
            f"\n\n【系统】按用户已说内容，当前仍缺：{' → '.join(pre_missing)}。"
            f"若需要追问，请优先问「{pre_missing[0]}」。"
            "问算法类别时必须用业务结果形态提问，禁止直接问分类/回归等术语。"
        )
    else:
        gap_note = "\n\n【系统】用户侧关键信息已较完整，优先 status=updated，不要再追问。"

    system = (
        _INTAKE_SYSTEM
        + f"\n\n当前垂域 domain={domain or 'generic'}。"
        + limit_note
        + gap_note
        + _build_snapshot_section(snapshot)
        + "\n\n当前表单 partial_form（JSON）：\n"
        + json.dumps(partial, ensure_ascii=False)[:3000]
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for item in memory.to_list():
        role = item.get("role", "user")
        content = item.get("content") or ""
        if content:
            messages.append({"role": role, "content": content})

    llm = LLM(config.llm)
    try:
        resp = await llm.complete(messages, temperature=0.3)
        raw = resp.content or ""
    except Exception as exc:
        logger.warning(f"aml_scenario_intake LLM 失败: {exc}")
        # 模型超时/不可用时降级：用规则补全本轮答案，避免直接 500
        result = _fallback_without_llm(
            sid=sid,
            text=text,
            partial=partial,
            snapshot=snapshot,
            at_limit=at_limit,
            exc=exc,
            user_corpus=_user_corpus_from_memory(memory),
        )
        memory.add(
            Message(
                role=Role.ASSISTANT,
                content=json.dumps(
                    {
                        "status": result.get("status"),
                        "text": result.get("text"),
                        "formDraft": result.get("formDraft") or {},
                        "degraded": True,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        await memory.persist()
        return result

    memory.add(Message(role=Role.ASSISTANT, content=raw))
    await memory.persist()

    parsed = _parse_json_blob(raw)
    if not parsed:
        if at_limit:
            draft = _ensure_narrative_from_partial(partial, {}, text)
            return {
                "status": "updated",
                "text": "已根据现有信息整理需求，您可在右侧补充细节后生成算法模型。",
                "session_id": sid,
                "formDraft": draft,
                "changedFields": list(draft.keys()),
            }
        return {
            "status": "question",
            "text": _slot_question("algorithm_category"),
            "hint": _slot_hint("algorithm_category"),
            "session_id": sid,
            "formDraft": {},
            "changedFields": [],
        }

    status = str(parsed.get("status") or "updated").strip().lower()
    if status not in ("updated", "question"):
        status = "updated"

    draft_raw = parsed.get("formDraft") if isinstance(parsed.get("formDraft"), dict) else {}
    user_corpus = _user_corpus_from_memory(memory)
    # 先用用户原话修补漏填；类别未业务确认前会去掉过早写入的 algorithm_category
    draft_raw = _enrich_draft_from_user_text(partial, draft_raw, text, user_corpus)
    form_draft, auto_changed = _sanitize_form_draft(draft_raw, snapshot, partial)
    enriched = _enrich_draft_from_user_text(partial, form_draft, text, user_corpus)
    if enriched != form_draft:
        form_draft, more_changed = _sanitize_form_draft(enriched, snapshot, partial)
        for key in more_changed:
            if key not in auto_changed:
                auto_changed.append(key)

    # 类别未确认时，确保不把 algorithm_category 回写给前端
    if not _has_category_confirm_signal(user_corpus):
        form_draft.pop("algorithm_category", None)
        auto_changed = [k for k in auto_changed if k != "algorithm_category"]

    claimed = parsed.get("changedFields")
    if isinstance(claimed, list) and claimed:
        changed_fields = [str(x) for x in claimed if str(x) in form_draft or str(x) in auto_changed]
        for key in form_draft:
            if key not in changed_fields:
                changed_fields.append(key)
    else:
        changed_fields = list(auto_changed)
    changed_fields = [k for k in changed_fields if k in form_draft or k in auto_changed]
    if not _has_category_confirm_signal(user_corpus):
        changed_fields = [k for k in changed_fields if k != "algorithm_category"]

    reply = str(parsed.get("text") or "").strip()
    hint = str(parsed.get("hint") or "").strip()

    missing = _assess_slots(partial, form_draft, user_corpus)

    def _force_category_question(current_reply: str) -> str:
        cat_q = _slot_question("algorithm_category")

        def _algo_not_system(s: str) -> str:
            return (
                (s or "")
                .replace("希望系统", "希望算法")
                .replace("系统最终帮", "算法最终帮")
                .replace("系统最后帮", "算法最后帮")
                .replace("系统帮您", "算法帮您")
                .replace("系统帮你", "算法帮你")
            )

        if not current_reply:
            return cat_q
        # 保留一句理解性确认，但去掉「问输入」的后半段
        if _reply_asks_about_input(current_reply) or "依据" in current_reply:
            head = current_reply.split("？")[0].split("?")[0]
            if "。" in head:
                ack = head.split("。")[0].strip() + "。"
                if ack and not _reply_asks_about_input(ack):
                    return _algo_not_system(ack) + cat_q
            return cat_q
        if any(k in current_reply for k in ("①", "哪种结果", "打个结论", "得到哪种结果")):
            return _algo_not_system(current_reply)
        if current_reply.endswith("？") or current_reply.endswith("?"):
            # 问的是别的（如输入），改问类别
            return cat_q
        return _algo_not_system(current_reply.rstrip("。") + "。" + cat_q)

    # 达上限：强制收敛
    if at_limit:
        status = "updated"
        form_draft = _ensure_narrative_from_partial(partial, form_draft, text)
        if "free_narrative" in form_draft and "free_narrative" not in changed_fields:
            changed_fields.append("free_narrative")
        reply = (
            reply
            if reply and "追问" not in reply
            else "已根据现有信息整理需求与配置，您可在右侧补充细节，确认后点击生成算法模型。"
        )
        # 若 LLM 仍给了问题句，改成收敛话术
        if reply.endswith("？") or reply.endswith("?"):
            reply = "已根据现有信息整理需求与配置，您可在右侧补充细节，确认后点击生成算法模型。"
        hint = ""
    elif missing and missing[0] == "algorithm_category":
        status = "question"
        reply = _force_category_question(reply)
        hint = hint if (hint and not _reply_asks_about_input(hint)) else _slot_hint("algorithm_category")
    elif status == "updated" and missing:
        # 模型过早收敛：按用户原话缺口降级为追问
        status = "question"
        slot = missing[0]
        view_cat = str(
            (_merged_view(partial, form_draft).get("algorithm_category") or "")
        )
        slot_q = _slot_question(slot, view_cat)
        if not reply or not (reply.endswith("？") or reply.endswith("?")):
            reply = slot_q
        if not hint:
            hint = _slot_hint(slot, view_cat)
    elif status == "question" and missing:
        # 保留模型口语追问；若未形成问句，再回落到缺口模板
        view_cat = str(
            (_merged_view(partial, form_draft).get("algorithm_category") or "")
        )
        if not reply or not (reply.endswith("？") or reply.endswith("?")):
            reply = _slot_question(missing[0], view_cat)
            if not hint:
                hint = _slot_hint(missing[0], view_cat)
        # 若模型误用专业术语问类别，替换为业务话术
        if missing[0] == "algorithm_category" and any(
            w in reply for w in ("回归", "聚类", "classification", "regression", "算法类别")
        ):
            reply = _slot_question("algorithm_category", view_cat)
            hint = hint or _slot_hint("algorithm_category", view_cat)
        if missing[0] != "algorithm_category" and _reply_asks_about_input(reply) is False:
            pass
    elif status == "question" and not missing:
        # 用户侧关键点已齐（含刚补全的标签）：改为更新，避免继续追问
        status = "updated"
        form_draft = _ensure_narrative_from_partial(partial, form_draft, text)
        if "free_narrative" in form_draft and "free_narrative" not in changed_fields:
            changed_fields.append("free_narrative")
        if reply.endswith("？") or reply.endswith("?"):
            reply = "已记下您补充的信息，需求已较完整，请查看右侧叙述与上方配置，确认后即可生成。"
        else:
            reply = reply or "需求已较完整，请查看右侧叙述与上方配置，确认后即可生成算法模型。"

    if not reply:
        reply = (
            "已根据您的描述更新了需求与配置，请查看右侧叙述并确认上方选项。"
            if status == "updated"
            else "能补充一下关键输入数据和期望输出形式吗？"
        )

    out: dict[str, Any] = {
        "status": status,
        "text": reply,
        "session_id": sid,
        "formDraft": form_draft,
        "changedFields": changed_fields,
    }
    if hint and status == "question":
        out["hint"] = hint
    return out
