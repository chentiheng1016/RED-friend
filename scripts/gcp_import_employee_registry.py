#!/usr/bin/env python3
"""Import a RED employee_registry.json file into Cloud Firestore.

Uses the active gcloud user token so it does not require Application Default
Credentials on the local machine.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "var" / "data" / "employee_registry.json"


def _gcloud_access_token() -> str:
    proc = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout.strip()


_OPTIONAL_STRING_FIELDS = ("line_user_id", "telegram_user_id")


def _firestore_fields(email: str, info: dict) -> dict:
    fields = {
        "email": {"stringValue": email},
        "name": {"stringValue": str(info.get("name") or email)},
        "color": {"stringValue": str(info.get("color") or "")},
    }
    for key in _OPTIONAL_STRING_FIELDS:
        value = str(info.get(key) or "").strip()
        if value:
            fields[key] = {"stringValue": value}
    return fields


def _patch_document(
    *,
    project: str,
    database: str,
    collection: str,
    email: str,
    info: dict,
    token: str,
) -> None:
    doc_id = urllib.parse.quote(email, safe="")
    url = (
        f"https://firestore.googleapis.com/v1/projects/{project}/databases/"
        f"{urllib.parse.quote(database, safe='()')}/documents/"
        f"{urllib.parse.quote(collection, safe='')}/{doc_id}"
    )
    payload = json.dumps({"fields": _firestore_fields(email, info)}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Google Cloud project id")
    parser.add_argument("--database", default="(default)", help="Firestore database id")
    parser.add_argument("--collection", default="red_employee_registry", help="Firestore collection name")
    parser.add_argument("--input", default=str(DEFAULT_REGISTRY), help="employee_registry.json path")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    args = parser.parse_args()

    path = Path(args.input)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")

    entries = {
        email.lower().strip(): info
        for email, info in data.items()
        if isinstance(info, dict) and email.strip()
    }
    print(f"Import source: {path}")
    print(f"Firestore: projects/{args.project}/databases/{args.database}/documents/{args.collection}")
    print(f"Employees: {len(entries)}")
    if args.dry_run:
        for email, info in sorted(entries.items()):
            print(f"  {email:32s} {info.get('color', ''):8s} {info.get('name', '')}")
        return 0

    token = _gcloud_access_token()
    for email, info in sorted(entries.items()):
        _patch_document(
            project=args.project,
            database=args.database,
            collection=args.collection,
            email=email,
            info=info,
            token=token,
        )
        print(f"imported {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
