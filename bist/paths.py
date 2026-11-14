"""Filesystem locations shared by every pipeline, resolved from the repository root."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_MAPPINGS_DIR = PROJECT_ROOT / "data_mappings"
MIGRATIONS_DIR = PROJECT_ROOT / "database" / "migrations"

# Bulk downloads; both folders are ignored by Git.
FINANCIAL_STATEMENTS_DIR = PROJECT_ROOT / "financial_statements"
ACTIVITY_REPORTS_DIR = PROJECT_ROOT / "activity_reports"
