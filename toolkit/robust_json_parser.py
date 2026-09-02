"""
稳健的 JSON 提取器 —— 用于从大模型输出中提取结构化数据

设计原则：
  1. 每一层容错都对应一个真实的线上故障，不做预防性设计
  2. 容错的目的是「保留归因能力」，不是「让流程不报错」
  3. 错误码必须能区分责任方，否则每次排查都要从头查一遍

错误码约定：
  15013 - 上游节点未返回数据      → 链路/配置问题，找研发
  15011 - 模型返回内容为空        → Prompt/输入问题，找产品
  15012 - 内容无法解析为 JSON     → 格式约束问题，Prompt + 解析双侧优化

详细的演进过程见 03-robustness/robust-parsing.md
"""

import re
import json
from typing import Any, Dict

# 推理模型的思考块标记。不同模型厂商的标记可能不同，按需扩展。
THINK_BLOCK_PATTERN = re.compile(r'<think>[\s\S]*?</think>', re.IGNORECASE)

# 贪婪匹配第一个 { 到最后一个 }。
# 注意：若模型可能返回多个独立 JSON 对象，改用非贪婪或括号配对计数。
JSON_OBJECT_PATTERN = re.compile(r'\{[\s\S]*\}')


def extract_json_object(raw_input: Any,
                        default: Dict = None,
                        result_key: str = "result") -> Dict:
    """
    从模型输出中提取 JSON 对象。

    Args:
        raw_input:  上游节点传入的原始内容（可能是 str / dict / None）
        default:    解析失败时 result 字段的默认值
        result_key: 输出中承载结果的字段名

    Returns:
        {
            <result_key>: dict,    # 解析出的对象，失败时为 default
            "isError":    "0"|"1",
            "errorMsg":   str,
            "errorCode":  str,
        }
    """
    default = {} if default is None else default

    # ---- Layer 1: 上游数据存在性 ----
    # None 和 "" 是两个不同的问题，必须分开：
    #   None → 上游根本没执行/没返回，是链路问题
    #   ""   → 模型执行了但没产出，是 Prompt 或输入问题
    if raw_input is None:
        return _build_error(default, result_key, "15013", "上游节点未返回数据")

    # ---- Layer 2: 类型防御 ----
    # 工作流平台传下来的类型并不稳定：
    # 有的平台会自动反序列化成 dict，有的传原始字符串
    raw = (raw_input if isinstance(raw_input, str) else str(raw_input)).strip()

    # ---- Layer 3: 剥离推理模型的思考过程 ----
    # 必须在空值检查之前执行。
    # 原因：存在「只输出了思考过程，没输出实际内容」的情况，
    #      此时 raw 非空但 cleaned 为空。顺序错了会归因到 15012 而非 15011。
    cleaned = THINK_BLOCK_PATTERN.sub('', raw).strip()

    # ---- Layer 4: 空内容检查 ----
    if not cleaned:
        return _build_error(default, result_key, "15011", "模型未返回有效内容")

    # ---- Layer 5: 抽取首个 JSON 对象 ----
    # 不要求整个返回值是合法 JSON。
    # 模型常在 JSON 前后附加解释性文字（「好的，分析结果如下：」）。
    match = JSON_OBJECT_PATTERN.search(cleaned)
    if not match:
        return _build_error(default, result_key, "15012", "返回内容中未找到 JSON 结构")

    # ---- Layer 6: 解析 ----
    try:
        obj = json.loads(match.group(0))
    except Exception as exc:
        return _build_error(default, result_key, "15012", f"JSON 解析失败: {exc}")

    # ---- Layer 7: 类型确认 ----
    # json.loads 成功不代表拿到的是 dict。
    # "[1,2,3]" 和 "123" 都能成功解析，但类型不是下游期望的。
    if not isinstance(obj, dict):
        return _build_error(
            default, result_key, "15012",
            f"解析结果类型异常，期望 object 实际 {type(obj).__name__}"
        )

    return {
        result_key: obj,
        "isError": "0",
        "errorMsg": "",
        "errorCode": "",
    }


def _build_error(default: Dict, result_key: str,
                 code: str, msg: str) -> Dict:
    return {
        result_key: default,
        "isError": "1",
        "errorMsg": msg,
        "errorCode": code,
    }


# ============================================================
# 工作流平台节点入口示例
# ============================================================
def main(arg1) -> dict:
    """
    ⚠️ 重要：输出变量的类型声明必须同步修改！

    在工作流平台上，节点输出变量需要显式声明类型。
    如果 result 仍声明为 string，平台会把 dict 序列化回字符串传给下游，
    下游又得再解析一次，等于白做。

    正确的类型声明：
        result     → object   ← 不是 string！
        isError    → string
        errorMsg   → string
        errorCode  → string

    这个坑的排查体验极差：代码正确、日志正确，但下游拿不到对象。
    排查方向应该是「数据在哪一层被改变了形态」，而不是「代码哪里写错了」。
    """
    return extract_json_object(arg1, default={}, result_key="result")


# ============================================================
# 测试用例 —— 覆盖 7 种真实失败模式
# ============================================================
if __name__ == "__main__":

    CASES = [
        # (说明, 输入, 期望 errorCode)
        ("上游未返回数据",
         None, "15013"),

        ("模型返回空字符串",
         "", "15011"),

        ("模型只输出了思考过程，无实际内容",
         "<think>让我想想这个商品的定位...</think>", "15011"),

        ("纯文本，无 JSON 结构",
         "抱歉，我无法识别这张图片中的商品信息。", "15012"),

        ("JSON 格式错误（尾随逗号）",
         '{"category": "美妆", "audience": "都市女性",}', "15012"),

        ("解析出的是数组而非对象",
         '["美妆", "护肤"]', "15012"),

        ("正常 JSON",
         '{"category": "美妆", "audience": "25-35岁都市女性"}', ""),

        ("JSON 前后带解释性文字",
         '好的，分析结果如下：\n\n{"category": "美妆"}\n\n希望对你有帮助。', ""),

        ("带思考块 + 正常 JSON",
         '<think>这是一个美妆产品，目标人群应该是...</think>\n'
         '{"category": "美妆", "audience": "25-35岁都市女性"}', ""),

        ("多行格式化 JSON",
         '{\n  "category": "美妆",\n  "audience": "都市女性"\n}', ""),

        ("上游传的是 dict 而非 str",
         {"category": "美妆"}, ""),
    ]

    passed = failed = 0
    for desc, payload, expected_code in CASES:
        out = extract_json_object(payload)
        actual_code = out["errorCode"]
        ok = actual_code == expected_code

        if ok:
            passed += 1
            mark = "PASS"
        else:
            failed += 1
            mark = "FAIL"

        print(f"[{mark}] {desc}")
        print(f"       期望码: {expected_code or '(无错误)'}  "
              f"实际码: {actual_code or '(无错误)'}")
        if not ok:
            print(f"       输出: {out}")
        print()

    print("=" * 50)
    print(f"通过 {passed} / {passed + failed}")
