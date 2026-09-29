#!/usr/bin/env python3
"""Safely create the local .env file without showing keys on screen."""

import getpass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / ".env"


def main():
    if TARGET.exists():
        answer = input("A .env file already exists. Replace it? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("No changes made.")
            return

    scopus = getpass.getpass("Paste SCOPUS_API_KEY (input is hidden): ").strip()
    simpler = getpass.getpass("Paste SIMPLER_GRANTS_API_KEY (input is hidden): ").strip()
    openai = getpass.getpass("Paste OPENAI_API_KEY (input is hidden): ").strip()
    if not scopus or not openai:
        raise SystemExit("Scopus and OpenAI keys are required. No file was written.")

    content = (
        f"SCOPUS_API_KEY={scopus}\n"
        f"SIMPLER_GRANTS_API_KEY={simpler}\n"
        f"OPENAI_API_KEY={openai}\n"
        "OPENAI_MODEL=gpt-6-astra\n"
        "OPENAI_EMBEDDING_MODEL=text-embedding-3-large\n"
        "SCOPUS_INST_TOKEN=\n"
    )
    TARGET.write_text(content, encoding="utf-8")
    os.chmod(TARGET, 0o600)
    print("Saved API settings to .env. This file is excluded from Git by .gitignore.")


if __name__ == "__main__":
    main()
