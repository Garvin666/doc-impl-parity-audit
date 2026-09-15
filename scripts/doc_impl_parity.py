#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc_impl_parity.py — 文档声明 <-> 实现声明 的双向差集（零依赖，启发式）

用途
    改完带文档的 CLI / MCP 工具 / skill 后，把「文档里声明的东西」与
    「代码里真有的东西」摊开对比，抓出幽灵命令、幽灵参数、字段名不一致、文档漏写。

用法
    python doc_impl_parity.py --doc SKILL.md --impl scripts/tool.py
    python doc_impl_parity.py --doc "docs/*.md" --impl "src/*.py" --json report.json

退出码
    0 = 双向无差集
    1 = 检出差集（详见报告）
    2 = 不可判定（某侧抽取为空：路径写错 / 格式不认识 / 无法读取）

设计纪律
    - 抽取不到东西一律退出码 2，绝不输出"全绿"（否则"没抽到"会被误当成"没问题"）
    - 脚本只负责摊开对比，不下"是否有问题"的结论；差集须人工确认再采信
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------- 正则与桶定义

# 文档侧
RE_CODE_SPAN = re.compile(r"`([^`\n]+)`")          # 反引号 token
RE_FENCED = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)```", re.S)   # 围栏代码块
RE_INDENTED = re.compile(r"(?:^[ \t]{4,}\S.*(?:\n|$))+", re.M)        # 缩进代码块
RE_TABLE_CELL = re.compile(r"^\s*\|(.+?)\|\s*$", re.M)  # markdown 表格行
RE_FLAG = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")
RE_SCRIPT_SUBCMD = re.compile(r"([A-Za-z_]\w*\.py)\s+([a-z][a-z0-9-]{2,})")
RE_SNAKE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")      # 至少一个下划线
RE_DOTTED = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
RE_CJK_WORD = re.compile(r"^[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9]{1,7}$")

# 文件 / 路径形态的 token：它们是"引用的文件"，不是字段名，必须排除出 field 桶
RE_PATH_TOKEN = re.compile(r"\.(py|md|json|ya?ml|txt|csv|xlsx?|docx?|pptx?|toml|ini|cfg|log|sh|bat|ps1|html?|parquet)$",
                           re.I)
# 文档里引用的脚本文件名（用于检查本轮 impl 覆盖是否完整）
RE_PY_REF = re.compile(r"\b([A-Za-z_]\w*\.py)\b")
# 生成物路径片段（形如 a/b/c）也不是字段名
RE_PATHLIKE = re.compile(r"[/\\]")

# 实现侧
RE_ADD_ARGUMENT = re.compile(r"""add_argument\(\s*["'](--[A-Za-z][A-Za-z0-9-]*)["']""")
RE_ADD_PARSER = re.compile(r"""add_parser\(\s*["']([A-Za-z0-9_-]+)["']""")
RE_FLAG_LITERAL = re.compile(r"""["'](--[A-Za-z][A-Za-z0-9-]*)["']""")
RE_UPPER_TUPLE = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=\n]+)?=\s*\(([^)]*)\)", re.M | re.S)
RE_STR_LITERAL = re.compile(r"""["']([^"'\n]{1,40})["']""")

BUCKETS = ("flag", "subcmd", "field")
BUCKET_TITLE = {
    "flag": "CLI 参数（--flag）",
    "subcmd": "子命令",
    "field": "字段名 / 枚举值（常量元组）",
}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def expand(patterns) -> list:
    """展开 glob；按顺序去重保留。"""
    out, seen = [], set()
    for pat in patterns or []:
        hits = glob.glob(pat) if any(ch in pat for ch in "*?[") else (
            [pat] if os.path.isfile(pat) else [])
        for h in sorted(hits):
            ap = os.path.abspath(h)
            if ap not in seen:
                seen.add(ap)
                out.append(ap)
    return out


# ---------------------------------------------------------------- 抽取：文档侧

def extract_doc(path: str):
    """返回 {bucket: {claim: [来源位置, ...]}}, 以及 token 统计。

    口径说明（这是控噪的关键）：文档侧**只从反引号 token 抽**。
    表格里裸写的中文（如"行为""质量""通过"）不是字段声明，抽进来会制造海量假阳性，
    把真线索淹掉 —— 实测该口径下差集从 131 项降到个位数量级。
    被反引号包裹的表格单元格仍会被全局反引号扫描覆盖，不会漏。
    """
    text = _read(path)
    found = {b: {} for b in BUCKETS}

    def add(bucket: str, claim: str, where: str):
        claim = claim.strip()
        if not claim:
            return
        found[bucket].setdefault(claim, [])
        if where not in found[bucket][claim]:
            found[bucket][claim].append(where)

    def line_of(needle: str) -> str:
        idx = text.find(needle)
        return f"L{text[:idx].count(chr(10)) + 1}" if idx >= 0 else "?"

    tokens = RE_CODE_SPAN.findall(text)
    for tok in tokens:
        tok = tok.strip()
        for fl in RE_FLAG.findall(tok):
            add("flag", fl, line_of(tok))
        m = RE_SCRIPT_SUBCMD.search(tok)
        if m:
            add("subcmd", m.group(2), line_of(tok))
        if tok.startswith("--"):          # 单独写的 --flag，不当字段名
            continue
        if RE_PATH_TOKEN.search(tok) or RE_PATHLIKE.search(tok):
            continue                      # 文件名 / 路径不是字段名（实测能砍掉一大类假阳性）
        if RE_SNAKE.match(tok) or RE_DOTTED.match(tok):
            add("field", tok, line_of(tok))
        elif RE_CJK_WORD.match(tok):
            add("field", tok, line_of(tok))

    # 代码块（围栏 + 缩进）—— 命令示例的主要载体。
    # 只在块内抽 flag / 子命令，不抽 field：缩进块也可能是子列表，抽普通词会灌入噪声。
    # 漏掉这段的代价很大：实测把"写在缩进块里的幽灵参数"整个漏检（假阴性）。
    blocks = [m.group(1) for m in RE_FENCED.finditer(text)]
    blocks += [m.group(0) for m in RE_INDENTED.finditer(text)]
    n_block_flags = 0
    for blk in blocks:
        for fl in RE_FLAG.findall(blk):
            n_block_flags += 1
            add("flag", fl, line_of(blk.strip()[:40]) if blk.strip() else "?")
        for m in RE_SCRIPT_SUBCMD.finditer(blk):
            add("subcmd", m.group(2), line_of(blk.strip()[:40]) if blk.strip() else "?")

    stats = {"backtick_tokens": len(tokens), "codeblocks": len(blocks),
             "block_flags": n_block_flags, "chars": len(text)}
    return found, stats


# ---------------------------------------------------------------- 抽取：实现侧

def extract_impl(path: str):
    text = _read(path)
    found = {b: {} for b in BUCKETS}

    def add(bucket: str, claim: str, where: str):
        claim = claim.strip()
        if claim:
            found[bucket].setdefault(claim, [])
            if where not in found[bucket][claim]:
                found[bucket][claim].append(where)

    # flag
    for src, rex in (("add_argument", RE_ADD_ARGUMENT), ("字面量", RE_FLAG_LITERAL)):
        for fl in rex.findall(text):
            add("flag", fl, src)

    # 子命令
    for name in RE_ADD_PARSER.findall(text):
        add("subcmd", name, "add_parser")

    # 字段名 / 枚举值
    # 口径一（窄）：UPPER_CASE 常量元组里的字面量 —— 声明性最强
    const_names = []
    for const, body in RE_UPPER_TUPLE.findall(text):
        const_names.append(const)
        for lit in RE_STR_LITERAL.findall(body):
            lit = lit.strip()
            if RE_CJK_WORD.match(lit) or RE_SNAKE.match(lit):
                add("field", lit, f"常量 {const}")

    # 口径二（补充）：全文的【中文字符串字面量】。
    # 必要性：真字段常以普通字面量出现而非常量元组，例如 meta 键就读写成
    # meta.get("熔断状态") —— 只抽口径一会把它误报成"文档写了、实现没有"。
    # 只用中文词限制爆炸半径；英文标识符仍只认口径一。
    for lit in RE_STR_LITERAL.findall(text):
        lit = lit.strip()
        if RE_CJK_WORD.match(lit):
            add("field", lit, "中文字面量")

    stats = {
        "add_argument": len(RE_ADD_ARGUMENT.findall(text)),
        "add_parser": len(RE_ADD_PARSER.findall(text)),
        "upper_consts": const_names,
        "cjk_literals": len([x for x in RE_STR_LITERAL.findall(text) if RE_CJK_WORD.match(x.strip())]),
    }
    return found, stats


# ---------------------------------------------------------------- 比对与报告

def diff_bucket(doc_map, impl_map):
    doc_only = sorted(set(doc_map) - set(impl_map))
    impl_only = sorted(set(impl_map) - set(doc_map))
    return doc_only, impl_only


def main() -> int:
    _force_utf8()

    ap = argparse.ArgumentParser(description="文档声明 <-> 实现声明 双向差集（启发式）")
    ap.add_argument("--doc", nargs="+", required=True, help="文档文件（支持 glob）")
    ap.add_argument("--impl", nargs="+", required=True, help="实现文件（支持 glob）")
    ap.add_argument("--json", dest="json_out", help="把结构化结果写到该路径")
    ap.add_argument("--ignore-doc-only", nargs="*", default=[],
                    help="屏蔽这些词（文档侧差集噪声，如通用词汇）")
    args = ap.parse_args()

    doc_paths, impl_paths = expand(args.doc), expand(args.impl)
    if not doc_paths or not impl_paths:
        print("[UNKNOWN] 文档或实现一侧未匹配到任何文件："
              f"doc={args.doc} -> {len(doc_paths)} 个，impl={args.impl} -> {len(impl_paths)} 个", file=sys.stderr)
        return 2

    ignore = set(args.ignore_doc_only)
    # argparse 无条件自建 --help/-h，实现里当然搜不到它们对应的 add_argument，
    # 不屏蔽则每次刷假阳性。从 argparse 自身取名单而非硬编码。
    # 注意 --version 不在此列：它必须被显式声明才存在，缺了就是真差集。
    ignore |= set(argparse.ArgumentParser()._option_string_actions)

    doc_all = {b: {} for b in BUCKETS}
    impl_all = {b: {} for b in BUCKETS}
    doc_stats, impl_stats = {}, {}

    for p in doc_paths:
        got, st = extract_doc(p)
        doc_stats[os.path.relpath(p)] = st
        for b in BUCKETS:
            for k, locs in got[b].items():
                doc_all[b].setdefault(k, [])
                for loc in locs:
                    entry = f"{os.path.relpath(p)}:{loc}"
                    if entry not in doc_all[b][k]:
                        doc_all[b][k].append(entry)

    for p in impl_paths:
        got, st = extract_impl(p)
        impl_stats[os.path.relpath(p)] = st
        for b in BUCKETS:
            for k, locs in got[b].items():
                impl_all[b].setdefault(k, [])
                for loc in locs:
                    entry = f"{os.path.relpath(p)} ({loc})"
                    if entry not in impl_all[b][k]:
                        impl_all[b][k].append(entry)

    total_doc = sum(len(doc_all[b]) for b in BUCKETS)
    total_impl = sum(len(impl_all[b]) for b in BUCKETS)
    if total_doc == 0 or total_impl == 0:
        print("[UNKNOWN] 抽取结果为空，无法判定 —— "
              f"文档侧抽到 {total_doc} 项，实现侧抽到 {total_impl} 项。"
              "检查路径是否写对、格式是否为所支持的形态。", file=sys.stderr)
        return 2

    print("=== 文档 <-> 实现 声明差集（启发式，差集须人工确认） ===")
    print(f"文档：{', '.join(os.path.relpath(p) for p in doc_paths)}")
    for name, st in doc_stats.items():
        print(f"      {name} -> 反引号 token {st['backtick_tokens']}，代码块 {st['codeblocks']}"
              f"（其中 flag {st['block_flags']}），字符 {st['chars']}")
    print(f"实现：{', '.join(os.path.relpath(p) for p in impl_paths)}")
    for name, st in impl_stats.items():
        print(f"      {name} -> add_argument {st['add_argument']}，add_parser {st['add_parser']}，"
              f"常量元组 {len(st['upper_consts'])} 个 {st['upper_consts']}")

    # impl_only 方向的判据用【文档全文 substring】，而非"文档反引号抽取结果"。
    # 理由：文档不会把每个参数/枚举值都用反引号列出来，用抽取集比会把大量
    # 实际写在正文里的名字误报成"文档未写"（实测假阳性 24/24）。
    # 该口径偏保守（宁可少报），符合"不制造噪音"的要求。
    doc_full_text = "\n".join(_read(p) for p in doc_paths)

    # 覆盖完整性：文档引用了哪些脚本、但本轮没传进 --impl？
    # 不报这一条，别的脚本的参数就会被误读成"文档写了、实现没有" —— 那是覆盖缺口，不是真差集。
    impl_basenames = {os.path.basename(p) for p in impl_paths}
    missing_impl = sorted({r for r in RE_PY_REF.findall(doc_full_text) if r not in impl_basenames})
    if missing_impl:
        print("\n[覆盖不完整] 文档引用了下列脚本，但它们未参与本轮比对：")
        for r in missing_impl:
            print(f"    {r}")
        print("    注意：上列脚本的参数会被误报成「文档写了、实现没有」——那是覆盖缺口，不是真差集。"
              "补齐 --impl 后重跑再采信。")

    report = {"doc": doc_stats, "impl": impl_stats, "coverage_gap": missing_impl, "buckets": {}}

    CONFIDENCE = {"flag": "高", "subcmd": "高", "field": "低（须人工筛）"}
    # 退出码只由【文档写了、实现没有】驱动 —— 这是唯一"照文档执行会出事"的方向。
    # 【实现有、文档未写】一律只作参考：文档本来就不必穷举所有参数与内部常量
    # （实测该项在真实项目里稳定刷出几十条，若参与判定会让工具长期假红而失去意义）。
    n_high = 0
    n_ref = 0

    for b in BUCKETS:
        doc_only, impl_only = diff_bucket(doc_all[b], impl_all[b])
        doc_only = [d for d in doc_only if d not in ignore]
        impl_only = [i for i in impl_only if i not in ignore and i not in doc_full_text]

        # field 桶里只有【中文短词】才可能是"字段名/枚举值"声明。英文标识符在文档里
        # 绝大多数是函数名、模块名、域名（实测占该桶 doc_only 的 84%），归参考项。
        if b == "field":
            high_only = [d for d in doc_only if RE_CJK_WORD.match(d)]
            ref_doc = [d for d in doc_only if d not in high_only]
        else:
            high_only, ref_doc = doc_only, []

        report["buckets"][b] = {"doc_only": doc_only, "doc_only_high": high_only,
                                "doc_only_ref": ref_doc, "impl_only": impl_only}
        print(f"\n--- {BUCKET_TITLE[b]}   [置信度：{CONFIDENCE[b]}] ---")
        print(f"[文档写了、实现没有] {len(high_only)} 项   <== 决定退出码")
        for k in high_only:
            print(f"    {k}    <- {', '.join(doc_all[b][k][:3])}")
        if ref_doc:
            print(f"[参考] 另 {len(ref_doc)} 项英文标识符（多为函数名/模块名/域名，非字段声明）：")
            for k in ref_doc[:8]:
                print(f"    {k}    <- {', '.join(doc_all[b][k][:1])}")
            if len(ref_doc) > 8:
                print(f"    ...（其余 {len(ref_doc) - 8} 项见 --json 输出）")
        print(f"[实现有、文档未写] {len(impl_only)} 项  (参考；判据：文档全文不含该词)")
        for k in impl_only:
            print(f"    {k}    <- {', '.join(impl_all[b][k][:3])}")
        n_high += len(high_only)
        n_ref += len(impl_only) + len(ref_doc)

    if n_ref:
        print("\n提示：【实现有、文档未写】是参考项 —— 文档不必穷举所有参数与内部常量"
              "（如 API 返回字段），通常无需处理。只关注其中的【工具接口契约】类（枚举值、必填字段）。")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\n结构化结果已写入：{args.json_out}")

    if n_high:
        print(f"\n=== 结果：文档承诺了 {n_high} 项实现里没有的东西（高置信，须逐项人工确认）===")
        if n_ref:
            print(f"    另有参考项 {n_ref} 项（实现有、文档未写；通常非问题）")
        return 1
    print("\n=== 结果：文档未承诺任何实现里没有的东西（幽灵命令/参数/字段 = 0）===")
    if n_ref:
        print(f"    另有参考项 {n_ref} 项（实现有、文档未写；通常非问题）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
