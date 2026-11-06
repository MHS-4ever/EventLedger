#!/usr/bin/env python3
"""Fetch runtime parameters without logging values or leaving partial files."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main():
    root = Path("/data/shipsafe")
    if not os.path.ismount(root) or not (root / ".shipsafe_ebs_mounted").is_file():
        raise ValueError("data mount unavailable")
    if "EBS_VOLUME_ID=${ebs_volume_id}" not in (root / ".shipsafe_ebs_mounted").read_text().splitlines():
        raise ValueError("incorrect EBS volume")
    result = subprocess.run([
        "aws", "ssm", "get-parameters-by-path", "--region", "${aws_region}",
        "--path", "/shipsafe/runtime/", "--with-decryption", "--output", "json",
    ], capture_output=True, text=True, check=True)
    values = {}
    for item in json.loads(result.stdout)["Parameters"]:
        name = item["Name"].removeprefix("/shipsafe/runtime/")
        value = item["Value"]
        # Match the controller's plain KEY=value parser and Compose dotenv safely.
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or item["Type"] != "SecureString":
            raise ValueError("invalid parameter metadata")
        if not value or value != value.strip() or any(c in value for c in "\r\n\x00$\"'`\\#"):
            raise ValueError("parameter value is incompatible with the runtime env format")
        values[name] = value
    required = {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "EVENTLEDGER_API_KEY", "EVENTLEDGER_SOURCE_ID"}
    if not required <= values.keys():
        raise ValueError("required parameters missing")
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix=".runtime.env.", dir=root)
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("".join(f"{name}={value}\n" for name, value in sorted(values.items())))
        os.replace(path, root / "runtime.env")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never include AWS stdout/stderr or parameter values in error messages.
        print("Runtime secret retrieval failed; check mount, IAM, KMS and parameter format.", file=sys.stderr)
        sys.exit(1)
