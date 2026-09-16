#!/usr/bin/env python3
"""Read-only paper discovery report.

Scans a vault root and prints a binding report WITHOUT writing anything to the
Vault or to the SQLite index. Intended for pre-flight inspection of a real
vault before any paper is adopted (ADR-006 discovery phase).

Usage:
    python -m backend.scripts.paper_discovery_report
    python -m backend.scripts.paper_discovery_report --root "/path/to/论文" --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from backend.app.paper.models import BindingState, MediaKind, SourceRole
from backend.app.paper.scanner import ScanConfig, discover_papers

DEFAULT_ROOT = (
    Path.home()
    / "Documents/xbpd_obsidian/02. 🟡 归类 Arrange/论文"
)


def build_report(root: Path, max_depth: int) -> dict:
    result = discover_papers(ScanConfig(root=root, max_depth=max_depth))

    role_counts: Counter = Counter()
    state_counts: Counter = Counter()
    multi_source = 0
    translation_variants = Counter()

    for paper in result.papers:
        state_counts[paper.binding_state.value] += 1
        for source in paper.sources:
            role_counts[source.role.value] += 1
        if len(paper.sources) > 2:
            multi_source += 1
        translations = sum(1 for s in paper.sources if s.role.is_translation)
        translation_variants[translations] += 1

    return {
        "root": str(root),
        "papers_total": len(result.papers) + len(result.ambiguous),
        "bound_ok": len(result.papers),
        "ambiguous": len(result.ambiguous),
        "mineru_containers": len(result.mineru_containers),
        "errors": result.errors,
        "binding_states": dict(state_counts),
        "source_roles": dict(role_counts),
        "papers_with_multiple_sources": multi_source,
        "translation_variant_histogram": dict(translation_variants),
        "ambiguous_samples": [
            {"folder": p.folder_relpath, "title": p.display_title}
            for p in result.ambiguous[:40]
        ],
        "multi_source_samples": [
            {
                "folder": p.folder_relpath,
                "title": p.display_title,
                "sources": [
                    {"role": s.role.value, "path": s.rel_path, "primary": s.is_primary}
                    for s in p.sources
                ],
            }
            for p in result.papers
            if len(p.sources) > 2
        ][:20],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only paper discovery report")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--json", dest="json_out", default=None)
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"scan root not found: {root}", file=sys.stderr)
        return 2

    report = build_report(root, args.max_depth)

    print("=" * 62)
    print("  论文工作台 · 只读发现报告 (Discovery Report)")
    print("=" * 62)
    print(f"  扫描根目录      : {report['root']}")
    print(f"  论文总数        : {report['papers_total']}")
    print(f"    已自动绑定    : {report['bound_ok']}")
    print(f"    待人工确认    : {report['ambiguous']}")
    print(f"  MinerU 产物容器 : {report['mineru_containers']}")
    print(f"  扫描错误        : {len(report['errors'])}")
    print()
    print("  绑定状态分布:")
    for state, count in sorted(report["binding_states"].items()):
        print(f"    {state:16} {count}")
    print()
    print("  来源角色分布:")
    for role, count in sorted(report["source_roles"].items()):
        print(f"    {role:20} {count}")
    print()
    print(f"  多来源论文(>2)  : {report['papers_with_multiple_sources']}")
    print("  翻译变体直方图  :")
    for count, papers in sorted(report["translation_variant_histogram"].items()):
        print(f"    {count} 个翻译来源 -> {papers} 篇论文")

    if report["ambiguous_samples"]:
        print()
        print("  待人工确认样例:")
        for item in report["ambiguous_samples"][:10]:
            print(f"    - {item['title']}  ({item['folder']})")

    if report["errors"]:
        print()
        print("  错误:")
        for err in report["errors"][:10]:
            print(f"    - {err}")

    if args.json_out:
        target = Path(args.json_out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print()
        print(f"  报告已写入: {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
