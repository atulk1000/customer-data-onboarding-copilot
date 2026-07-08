from __future__ import annotations

import re
import string
from collections.abc import Iterable
from typing import Any

import pandas as pd

from onboarding.schema import ENUM_NORMALIZERS

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS_RE = re.compile(r"\D+")
SPACE_RE = re.compile(r"\s+")

ABBREVIATIONS = {
    "mbr": "member",
    "mem": "member",
    "no": "number",
    "num": "number",
    "eff": "effective",
    "term": "termination",
    "sub": "subscriber",
    "rel": "relationship",
    "dt": "date",
}


def normalize_column_name(name: Any) -> str:
    value = "" if name is None else str(name)
    value = value.strip().lower()
    value = value.replace("_", " ").replace("-", " ").replace("/", " ")
    value = value.translate(str.maketrans("", "", string.punctuation.replace("_", "")))
    tokens = []
    for token in SPACE_RE.sub(" ", value).strip().split(" "):
        tokens.append(ABBREVIATIONS.get(token, token))
    return " ".join(token for token in tokens if token)


def normalize_token(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip().lower()
    text = text.replace("_", " ").replace("-", " ").replace("/", " ").replace(".", " ")
    text = text.translate(str.maketrans("", "", string.punctuation.replace("_", "")))
    return SPACE_RE.sub(" ", text).strip()


def _clean_non_null(series: pd.Series) -> pd.Series:
    as_text = series.dropna().map(lambda value: str(value).strip())
    return as_text[as_text.ne("")]


def _rate(mask: Iterable[bool], denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(sum(1 for value in mask if value) / denominator, 4)


def _date_parse(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype="datetime64[ns]")
    return pd.to_datetime(series, errors="coerce", format="mixed")


def _email_rate(values: pd.Series) -> float:
    return _rate((bool(EMAIL_RE.match(value)) for value in values), len(values))


def _phone_rate(values: pd.Series) -> float:
    def is_phone(value: str) -> bool:
        digits = PHONE_DIGITS_RE.sub("", value)
        return len(digits) in {10, 11}

    return _rate((is_phone(value) for value in values), len(values))


def _numeric_rate(values: pd.Series) -> float:
    if values.empty:
        return 0.0
    parsed = pd.to_numeric(values, errors="coerce")
    return round(float(parsed.notna().mean()), 4)


def _known_enum_matches(values: pd.Series) -> dict[str, list[str]]:
    normalized_values = {normalize_token(value) for value in values if str(value).strip()}
    matches: dict[str, list[str]] = {}
    for field, normalizer in ENUM_NORMALIZERS.items():
        found = sorted({normalizer[value] for value in normalized_values if value in normalizer})
        if found:
            matches[field] = found
    return matches


def _infer_type(
    normalized_name: str,
    values: pd.Series,
    unique_rate: float,
    date_parse_rate: float,
    email_pattern_rate: float,
    phone_pattern_rate: float,
    numeric_parse_rate: float,
    known_enum_matches: dict[str, list[str]],
) -> str:
    if email_pattern_rate >= 0.6:
        return "email"
    if phone_pattern_rate >= 0.6:
        return "phone"
    if date_parse_rate >= 0.7:
        return "date"
    if known_enum_matches:
        return "enum"
    name_tokens = set(normalized_name.split())
    if name_tokens.intersection({"first", "last", "middle", "name", "carrier", "payer"}):
        return "text"
    if name_tokens.intersection({"id", "number", "code"}):
        return "identifier"
    if numeric_parse_rate >= 0.85:
        return "numeric"
    if len(values) and unique_rate <= 0.1:
        return "enum"
    return "text"


def profile_dataframe(df: pd.DataFrame, sample_size: int = 5, top_n: int = 5) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    row_count = len(df)

    for column in df.columns:
        series = df[column]
        values = _clean_non_null(series)
        non_null_count = int(values.size)
        null_rate = round(1 - (non_null_count / row_count), 4) if row_count else 0.0
        unique_count = int(values.nunique(dropna=True))
        unique_rate = round(unique_count / non_null_count, 4) if non_null_count else 0.0
        date_values = _date_parse(values)
        date_parse_rate = round(float(date_values.notna().mean()), 4) if non_null_count else 0.0
        email_pattern_rate = _email_rate(values)
        phone_pattern_rate = _phone_rate(values)
        numeric_parse_rate = _numeric_rate(values)
        known_enum_matches = _known_enum_matches(values)
        normalized_name = normalize_column_name(column)
        inferred_type = _infer_type(
            normalized_name=normalized_name,
            values=values,
            unique_rate=unique_rate,
            date_parse_rate=date_parse_rate,
            email_pattern_rate=email_pattern_rate,
            phone_pattern_rate=phone_pattern_rate,
            numeric_parse_rate=numeric_parse_rate,
            known_enum_matches=known_enum_matches,
        )

        parsed_dates = date_values.dropna()
        min_date = parsed_dates.min().date().isoformat() if not parsed_dates.empty else None
        max_date = parsed_dates.max().date().isoformat() if not parsed_dates.empty else None

        top_values = {
            str(key): int(value)
            for key, value in values.value_counts(dropna=True).head(top_n).items()
        }
        sample_values = [str(value) for value in values.drop_duplicates().head(sample_size).tolist()]

        profiles.append(
            {
                "column_name": str(column),
                "normalized_name": normalized_name,
                "inferred_type": inferred_type,
                "non_null_count": non_null_count,
                "null_rate": null_rate,
                "unique_count": unique_count,
                "unique_rate": unique_rate,
                "sample_values": sample_values,
                "top_values": top_values,
                "date_parse_rate": date_parse_rate,
                "email_pattern_rate": email_pattern_rate,
                "phone_pattern_rate": phone_pattern_rate,
                "numeric_parse_rate": numeric_parse_rate,
                "known_enum_matches": known_enum_matches,
                "min_date": min_date,
                "max_date": max_date,
            }
        )

    return profiles


def profiles_to_dataframe(profiles: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [
        "column_name",
        "normalized_name",
        "inferred_type",
        "null_rate",
        "unique_count",
        "unique_rate",
        "date_parse_rate",
        "email_pattern_rate",
        "phone_pattern_rate",
        "numeric_parse_rate",
        "known_enum_matches",
    ]
    return pd.DataFrame(profiles)[columns] if profiles else pd.DataFrame(columns=columns)
