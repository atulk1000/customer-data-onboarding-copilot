from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "data" / "demo" / "messy_eligibility_file.csv"
RANDOM_SEED = 20260706
ROW_COUNT = 1000

FIRST_NAMES = [
    "Ana",
    "Jordan",
    "Sam",
    "Mia",
    "Noah",
    "Priya",
    "Elena",
    "Marcus",
    "Tara",
    "Owen",
    "Nina",
    "Luis",
    "Avery",
    "Riley",
    "Maya",
    "Evan",
    "Sofia",
    "Caleb",
    "Iris",
    "Leo",
]

LAST_NAMES = [
    "Patel",
    "Lee",
    "Rivera",
    "Chen",
    "Johnson",
    "Garcia",
    "Nguyen",
    "Smith",
    "Brown",
    "Khan",
    "Singh",
    "Davis",
    "Wilson",
    "Martinez",
    "Thomas",
    "Moore",
]

PLANS = [
    ("PPO-100", "Silver PPO", "PPO", "Acme Health"),
    ("HMO-200", "Basic HMO", "HMO", "Acme Health"),
    ("HDHP-300", "Saver HDHP", "HDHP", "Northstar Benefits"),
    ("EPO-400", "Metro EPO", "EPO", "Northstar Benefits"),
    ("POS-500", "Choice POS", "POS", "Contoso Care"),
]

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"]


def fmt_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.strftime(random.choice(DATE_FORMATS))


def random_dob(is_child: bool) -> date:
    if is_child:
        start = date(2008, 1, 1)
        end = date(2022, 12, 31)
    else:
        start = date(1960, 1, 1)
        end = date(1998, 12, 31)
    span = (end - start).days
    return start + timedelta(days=random.randint(0, span))


def random_effective_date() -> date:
    start = date(2023, 1, 1)
    end = date(2026, 1, 1)
    return start + timedelta(days=random.randint(0, (end - start).days))


def random_phone() -> str:
    digits = f"555{random.randint(1000000, 9999999)}"
    style = random.choice(["dash", "plain", "paren"])
    if style == "plain":
        return digits
    if style == "paren":
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def make_email(first: str, last: str, member_number: str) -> str:
    return f"{first.lower()}.{last.lower()}.{member_number[-4:]}@example.com"


def base_row(member_number: str, subscriber_id: str, relation: str, is_child: bool) -> dict[str, str]:
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    dob = random_dob(is_child=is_child)
    plan_code, plan_name, plan_type, carrier = random.choice(PLANS)
    effective = random_effective_date()
    is_terminated = random.random() < 0.16
    term_date = effective + timedelta(days=random.randint(60, 720)) if is_terminated else None
    status = (
        random.choice(["Termed", "T", "Terminated"]) if is_terminated else random.choice(["Active", "A", "Pending"])
    )
    sex = random.choice(["M", "F", "Male", "Female", "O"])

    return {
        "Member Number": member_number,
        "First": first,
        "Last": last,
        "DOB": fmt_date(dob),
        "Sex": sex,
        "Email Address": make_email(first, last, member_number) if random.random() > 0.08 else "",
        "Phone": random_phone(),
        "Plan Code": plan_code,
        "Plan Name": plan_name,
        "Plan Type": random.choice(
            [plan_type, plan_type.replace("PPO", "P.P.O"), "High Deductible" if plan_type == "HDHP" else plan_type]
        ),
        "Carrier": carrier,
        "Effective Date": fmt_date(effective),
        "Term Date": fmt_date(term_date),
        "Status": status,
        "Relation": relation,
        "Subscriber ID": subscriber_id,
    }


def apply_intentional_issue(row: dict[str, str], row_index: int, existing_member_numbers: list[str]) -> None:
    issue_roll = row_index % 37
    if issue_roll == 0:
        row["DOB"] = "1979-02-30"
    elif issue_roll == 1:
        row["Email Address"] = "not-an-email"
    elif issue_roll == 2:
        row["Phone"] = "abc"
    elif issue_roll == 3:
        row["Status"] = "UnknownStatus"
    elif issue_roll == 4:
        row["Relation"] = "Partner"
    elif issue_roll == 5:
        row["Plan Type"] = "MysteryPlan"
    elif issue_roll == 6:
        row["Effective Date"] = "2025-06-01"
        row["Term Date"] = "2024-01-01"
    elif issue_roll == 7 and row["Relation"] != "Self":
        row["Subscriber ID"] = ""
    elif issue_roll == 8 and row["Relation"] != "Self":
        row["Subscriber ID"] = "MEM999999"
    elif issue_roll == 9:
        row["DOB"] = "2035-01-01"
    elif issue_roll == 10:
        row["First"] = ""
    elif issue_roll == 11:
        row["Plan Code"] = ""
    elif issue_roll == 12 and existing_member_numbers:
        row["Member Number"] = random.choice(existing_member_numbers)
        row["DOB"] = "1962-03-14"
        row["Last"] = "Conflict"


def generate_rows() -> list[dict[str, str]]:
    random.seed(RANDOM_SEED)
    rows: list[dict[str, str]] = []
    existing_members: list[str] = []
    employee_index = 1

    while len(rows) < ROW_COUNT:
        member_number = f"MEM{employee_index:06d}"
        employee = base_row(member_number, member_number, "Self", is_child=False)
        apply_intentional_issue(employee, len(rows), existing_members)
        rows.append(employee)
        existing_members.append(member_number)

        dependent_count = random.choice([0, 1, 1, 2])
        for dependent_position in range(dependent_count):
            if len(rows) >= ROW_COUNT:
                break
            dep_number = f"DEP{employee_index:06d}{dependent_position + 1}"
            relation = random.choice(["Spouse", "Child", "Dependent"])
            dependent = base_row(dep_number, member_number, relation, is_child=relation != "Spouse")
            apply_intentional_issue(dependent, len(rows), existing_members)
            rows.append(dependent)
            existing_members.append(dep_number)

        employee_index += 1

    return rows[:ROW_COUNT]


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = generate_rows()
    fieldnames = [
        "Member Number",
        "First",
        "Last",
        "DOB",
        "Sex",
        "Email Address",
        "Phone",
        "Plan Code",
        "Plan Name",
        "Plan Type",
        "Carrier",
        "Effective Date",
        "Term Date",
        "Status",
        "Relation",
        "Subscriber ID",
    ]
    with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
