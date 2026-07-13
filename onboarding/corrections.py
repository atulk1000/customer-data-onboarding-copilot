from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd


class CorrectionValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CorrectionUploadResult:
    overlays: list[dict[str, Any]]
    errors: list[str]

    @property
    def is_valid(self) -> bool:
        return not self.errors and bool(self.overlays)


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return value
    if pd.isna(value):
        return None
    return value


def _same_value(left: Any, right: Any) -> bool:
    clean_left = _clean_value(left)
    clean_right = _clean_value(right)
    if clean_left is None and clean_right is None:
        return True
    return str(clean_left) == str(clean_right)


def source_record_id(source_file_hash: str, source_row_number: int) -> str:
    digest = hashlib.sha256(f"{source_file_hash}:{source_row_number}".encode()).hexdigest()
    return f"SRC-{digest[:20].upper()}"


def original_row_fingerprint(row: dict[str, Any]) -> str:
    clean = {str(key): _clean_value(value) for key, value in row.items()}
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_identity_for_row(
    source_df: pd.DataFrame,
    source_file_hash: str,
    source_row_number: int,
) -> dict[str, Any]:
    source_index = source_row_number - 2
    if source_index < 0 or source_index >= len(source_df):
        raise CorrectionValidationError(f"Source row number {source_row_number} is outside the source file.")
    raw_row = source_df.iloc[source_index].to_dict()
    return {
        "source_record_id": source_record_id(source_file_hash, source_row_number),
        "source_row_number": source_row_number,
        "original_row_fingerprint": original_row_fingerprint(raw_row),
    }


def add_correction_columns(
    rejected_rows: pd.DataFrame,
    source_df: pd.DataFrame,
    source_file_hash: str,
) -> pd.DataFrame:
    if rejected_rows.empty:
        columns = [
            "source_record_id",
            "source_row_number",
            "original_row_fingerprint",
            "correction_comment",
        ]
        return pd.DataFrame(columns=columns)

    records: list[dict[str, Any]] = []
    metadata_columns = [
        "row_status",
        "error_count",
        "error_codes",
        "error_target_fields",
        "error_source_columns",
        "errors",
        "warning_count",
        "warning_codes",
        "warning_target_fields",
        "warning_source_columns",
        "warnings",
    ]
    for row in rejected_rows.to_dict("records"):
        row_number = int(row["source_row_number"])
        identity = source_identity_for_row(source_df, source_file_hash, row_number)
        source_index = row_number - 2
        original_source_row = source_df.iloc[source_index]
        record: dict[str, Any] = {**identity}
        for column in metadata_columns:
            if column in row:
                record[column] = row.get(column)
        for source_column in source_df.columns:
            original_column = f"original__{source_column}"
            original_value = _clean_value(original_source_row.get(source_column))
            record[original_column] = original_value
            record[f"corrected__{source_column}"] = original_value
        record["correction_comment"] = ""
        records.append(record)
    return pd.DataFrame(records)


def validate_correction_upload(
    uploaded: pd.DataFrame,
    expected_work_queue: pd.DataFrame,
) -> CorrectionUploadResult:
    errors: list[str] = []
    overlays: list[dict[str, Any]] = []
    required_columns = {"source_record_id", "source_row_number", "original_row_fingerprint", "correction_comment"}
    missing_columns = sorted(required_columns - set(uploaded.columns))
    if missing_columns:
        return CorrectionUploadResult([], ["Missing correction columns: " + ", ".join(missing_columns) + "."])

    duplicate_ids = uploaded["source_record_id"].astype(str).duplicated(keep=False)
    if duplicate_ids.any():
        duplicate_values = sorted(set(uploaded.loc[duplicate_ids, "source_record_id"].astype(str)))
        errors.append("Duplicate source_record_id values: " + ", ".join(duplicate_values) + ".")

    expected_by_id = {str(row["source_record_id"]): row for row in expected_work_queue.to_dict("records")}
    corrected_columns = [column for column in uploaded.columns if column.startswith("corrected__")]
    if not corrected_columns:
        errors.append("Correction file does not contain any corrected__ source columns.")

    for row_index, row in uploaded.iterrows():
        record_id = str(row.get("source_record_id") or "").strip()
        expected = expected_by_id.get(record_id)
        display_row = int(row_index) + 2
        if expected is None:
            errors.append(f"Correction row {display_row} has unknown source_record_id {record_id or '(blank)' }.")
            continue
        try:
            source_row_number = int(row.get("source_row_number"))
        except (TypeError, ValueError):
            errors.append(f"Correction row {display_row} has an invalid source_row_number.")
            continue
        if source_row_number != int(expected["source_row_number"]):
            errors.append(f"Correction row {display_row} changed source_row_number for {record_id}.")
            continue
        fingerprint = str(row.get("original_row_fingerprint") or "")
        if fingerprint != str(expected["original_row_fingerprint"]):
            errors.append(f"Correction row {display_row} has an original row fingerprint mismatch.")
            continue

        changes: dict[str, Any] = {}
        for corrected_column in corrected_columns:
            source_column = corrected_column.removeprefix("corrected__")
            original_column = f"original__{source_column}"
            if original_column not in expected:
                errors.append(f"Correction row {display_row} references unknown source column {source_column}.")
                continue
            original_value = expected.get(original_column)
            corrected_value = row.get(corrected_column)
            if not _same_value(original_value, corrected_value):
                changes[source_column] = _clean_value(corrected_value)

        if not changes:
            continue
        comment = str(row.get("correction_comment") or "").strip()
        if not comment:
            errors.append(f"Correction row {display_row} requires correction_comment.")
            continue
        overlays.append(
            {
                "source_record_id": record_id,
                "source_row_number": source_row_number,
                "original_row_fingerprint": fingerprint,
                "changes": changes,
                "correction_reason": comment,
            }
        )

    if not overlays and not errors:
        errors.append("No corrected values were found in the correction file.")
    return CorrectionUploadResult(overlays=overlays, errors=errors)


def apply_correction_overlays(
    source_df: pd.DataFrame,
    source_file_hash: str,
    overlays: list[dict[str, Any]],
) -> pd.DataFrame:
    corrected = source_df.copy(deep=True)
    seen_ids: set[str] = set()
    for overlay in overlays:
        record_id = str(overlay.get("source_record_id") or "")
        if record_id in seen_ids:
            raise CorrectionValidationError(f"Duplicate correction overlay for {record_id}.")
        seen_ids.add(record_id)
        row_number = int(overlay["source_row_number"])
        identity = source_identity_for_row(source_df, source_file_hash, row_number)
        if record_id != identity["source_record_id"]:
            raise CorrectionValidationError(f"Source record identity mismatch for row {row_number}.")
        if str(overlay.get("original_row_fingerprint") or "") != identity["original_row_fingerprint"]:
            raise CorrectionValidationError(f"Original row fingerprint mismatch for row {row_number}.")
        source_index = row_number - 2
        for source_column, value in dict(overlay.get("changes") or {}).items():
            if source_column not in corrected.columns:
                raise CorrectionValidationError(f"Unknown corrected source column: {source_column}.")
            corrected.at[source_index, source_column] = value
    return corrected


def correction_audit_rows(
    overlays: list[dict[str, Any]],
    source_df: pd.DataFrame,
    *,
    corrected_by: str,
) -> list[dict[str, Any]]:
    audit_rows: list[dict[str, Any]] = []
    corrected_at = datetime.now().isoformat(timespec="seconds")
    for overlay in overlays:
        row_number = int(overlay["source_row_number"])
        source_index = row_number - 2
        for source_column, corrected_value in dict(overlay.get("changes") or {}).items():
            audit_rows.append(
                {
                    "source_record_id": overlay["source_record_id"],
                    "source_row_number": row_number,
                    "original_row_fingerprint": overlay["original_row_fingerprint"],
                    "source_column": source_column,
                    "original_value": _clean_value(source_df.iloc[source_index][source_column]),
                    "corrected_value": _clean_value(corrected_value),
                    "correction_reason": overlay.get("correction_reason") or "",
                    "correction_status": "pending_revalidation",
                    "corrected_by": corrected_by,
                    "corrected_at": corrected_at,
                }
            )
    return audit_rows
