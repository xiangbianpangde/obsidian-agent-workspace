#!/usr/bin/env python3
"""Index the paper vault into SQLite.

Discovery is identity-free by design (ADR-006): the scanner never writes to the
Vault and emits no paper_id. This indexer is what assigns identity, and it must
be idempotent — re-running it after adding papers must preserve the status,
workspace state and note bindings of every paper it already knew about.

Usage:
    python -m backend.scripts.paper_index            # index the configured vault
    python -m backend.scripts.paper_index --dry-run  # report only, write nothing
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from backend.app.config import load_config
from backend.app.paper.models import utc_now
from backend.app.paper import MANIFEST_FILENAME
from backend.app.paper.manifest import (
    load_adopted_identity,
    manifest_to_sources,
    read_manifest_file,
)
from backend.app.paper.scanner import ScanConfig, discover_papers
from backend.app.paper.storage import PaperStorage
from backend.app.paper.writer import VaultWriteService


def index_papers(dry_run: bool = False) -> dict:
    cfg = load_config()
    root = cfg.papers_root_or_default
    if not root.is_dir():
        raise SystemExit(f"papers root not found: {root}")

    result = discover_papers(ScanConfig(root=root, max_depth=cfg.papers_max_depth))
    storage = None if dry_run else PaperStorage()
    # The manifest is the identity authority (ADR-006). A rescan must reuse
    # the ids it records, otherwise a new source_id is minted on every index
    # run and reading positions, notes and annotations all lose their anchor.
    service = None if dry_run else VaultWriteService(cfg.vault_root)
    papers_root_rel = (
        str(root.relative_to(cfg.vault_root))
        if root != cfg.vault_root
        else ""
    )

    created = 0
    updated = 0
    sources_written = 0
    conflicts = 0
    result_errors: list[str] = []

    for paper in result.papers:
        parts = paper.folder_relpath.split("/")
        paper.category_relpath = parts[0] if len(parts) > 1 else ""

        if dry_run:
            created += 1
            sources_written += len(paper.sources)
            continue

        # Reuse the existing identity for this folder so a rescan preserves
        # status, workspace state and note bindings.
        # Identity precedence: the Vault manifest outranks the local row,
        # because the manifest is what survives losing the database.
        adopted = None
        try:
            adopted = load_adopted_identity(service, paper.folder_relpath, papers_root_rel)
        except Exception as exc:  # noqa: BLE001
            result_errors.append(f"manifest unreadable for {paper.folder_relpath}: {exc}")

        existing = storage.get_paper_by_folder(paper.folder_relpath)
        if adopted:
            paper.paper_id = adopted[0]
            paper.manifest_relpath = MANIFEST_FILENAME
        elif existing:
            paper.paper_id = existing.paper_id
        else:
            from backend.app.paper.models import new_paper_id

            paper.paper_id = new_paper_id()

        if existing:
            # Carry over runtime state that the scanner knows nothing about.
            paper.status = existing.status
            paper.first_opened_at = existing.first_opened_at
            paper.last_opened_at = existing.last_opened_at
            paper.completed_at = existing.completed_at
            paper.status_changed_at = existing.status_changed_at
            paper.note_id = existing.note_id
            paper.paper_tags = existing.paper_tags
            paper.created_at = existing.created_at
            paper.updated_at = utc_now()
            updated += 1
        else:
            created += 1

        if not storage.upsert_paper(paper, allow_folder_move=True):
            conflicts += 1
            continue

        # Bind sources, reusing existing source ids by relative path so the
        # PDF endpoint keeps serving the same source_id across rescans.
        existing_sources = {
            s.rel_path: s for s in storage.list_sources(paper.paper_id, include_inactive=True)
        }
        # Source ids recorded in the manifest are authoritative for the same
        # reason the paper id is.
        manifest_sources = (
            {s.rel_path: s for s in manifest_to_sources(paper.paper_id, adopted[1])}
            if adopted
            else {}
        )
        for source in paper.sources:
            from_manifest = manifest_sources.get(source.rel_path)
            if from_manifest is not None:
                # Keep the id the Vault recorded; re-derive the runtime facts.
                source.source_id = from_manifest.source_id
                prior = existing_sources.get(source.rel_path)
                source.source_version = prior.source_version if prior else 1
                if prior and prior.sha256 == source.sha256:
                    source.sha256 = prior.sha256
            if prior and from_manifest is None:
                # Reuse identity, but only inherit the version when the bytes
                # are actually unchanged. Blindly carrying source_version and
                # sha256 forward made every file look immutable: replacing a
                # PDF in place left version at 1, so `?version=1` kept serving
                # the new file and 412 could never fire (ADR-009).
                source.source_id = prior.source_id
                source.created_at = prior.created_at

                changed = (
                    prior.sha256 is None
                    or prior.sha256 != source.sha256
                    or (prior.size_bytes is not None and prior.size_bytes != source.size_bytes)
                    or (prior.mtime_ns is not None and prior.mtime_ns != source.mtime_ns)
                )
                if changed:
                    source.source_version = (prior.source_version or 1) + 1
                else:
                    source.source_version = prior.source_version or 1
                    source.sha256 = prior.sha256
            source.paper_id = paper.paper_id
            storage.upsert_source(source)
            sources_written += 1

        # Re-point the paper at whichever source ends up primary this run.
        primary_pdf = next(
            (s for s in paper.sources if s.is_primary and s.media_kind.value == "PDF"), None
        )
        if primary_pdf is not None:
            paper.primary_pdf_source_id = primary_pdf.source_id
        primary_tr = next((s for s in paper.sources if s.is_primary and s.role.is_translation), None)
        if primary_tr is not None:
            paper.primary_translation_source_id = primary_tr.source_id
        storage.upsert_paper(paper, allow_folder_move=True)

    states = Counter(p.binding_state.value for p in result.papers)
    return {
        "papers_found": len(result.papers),
        "ambiguous": len(result.ambiguous),
        "mineru_containers": len(result.mineru_containers),
        "created": created,
        "updated": updated,
        "sources": sources_written,
        "conflicts": conflicts,
        "binding_states": dict(states),
        "errors": result.errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index the paper vault")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args(argv)

    report = index_papers(dry_run=args.dry_run)

    print("=" * 58)
    print("  论文索引" + ("（dry-run，未写入）" if args.dry_run else ""))
    print("=" * 58)
    print(f"  发现论文      : {report['papers_found']}")
    print(f"  新增          : {report['created']}")
    print(f"  更新          : {report['updated']}")
    print(f"  来源绑定      : {report['sources']}")
    print(f"  身份冲突      : {report['conflicts']}")
    print(f"  待人工确认    : {report['ambiguous']}")
    print(f"  MinerU 容器   : {report['mineru_containers']}")
    print()
    print("  绑定状态分布:")
    for state, count in sorted(report["binding_states"].items()):
        print(f"    {state:16} {count}")
    if report["errors"]:
        print()
        print(f"  扫描错误 {len(report['errors'])} 条:")
        for err in report["errors"][:5]:
            print(f"    - {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
