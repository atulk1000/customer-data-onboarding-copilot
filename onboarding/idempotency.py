from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd


def source_dataframe_fingerprint(df: pd.DataFrame) -> str:
    csv_payload = df.to_csv(index=False, lineterminator="\n", na_rep="")
    return hashlib.sha256(csv_payload.encode("utf-8")).hexdigest()


def short_fingerprint(fingerprint: str) -> str:
    return fingerprint[:12]


def build_import_replay_check(engine: Any, source_file_hash: str) -> dict[str, Any]:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(
                text("""
                SELECT id, file_name, completed_at, status
                FROM import_runs
                WHERE source_file_hash = :source_file_hash
                ORDER BY id DESC
                LIMIT 1
                """),
                {"source_file_hash": source_file_hash},
            )
            .mappings()
            .first()
        )

    if row is None:
        return {
            "source_file_hash": source_file_hash,
            "source_file_hash_short": short_fingerprint(source_file_hash),
            "is_replay": False,
            "previous_import_run_id": None,
            "previous_file_name": "",
            "previous_completed_at": "",
            "previous_status": "",
            "message": "No previous publish found for this exact source file fingerprint.",
        }

    return {
        "source_file_hash": source_file_hash,
        "source_file_hash_short": short_fingerprint(source_file_hash),
        "is_replay": True,
        "previous_import_run_id": int(row["id"]),
        "previous_file_name": row["file_name"],
        "previous_completed_at": str(row["completed_at"] or ""),
        "previous_status": row["status"],
        "message": f"This exact source file fingerprint was already published as import run {row['id']}.",
    }
