import ast
import re
from pathlib import Path
from typing import Set

MIGRATIONS_DIR = Path(__file__).parent.parent / "supabase" / "migrations"
MIGRATION_FILE = MIGRATIONS_DIR / "20260829001000_mobile_system_health_rpc.sql"
SQL_TEST_FILE = Path(__file__).parent / "sql" / "mobile_system_health_rpc_test.sql"
JOBS_DIR = Path(__file__).parent.parent / "app" / "jobs"

EXPECTED_13_COLUMNS = [
    ("job_code", "text"),
    ("category", "text"),
    ("display_name", "text"),
    ("sort_order", "integer"),
    ("execution_status", "text"),
    ("health_status", "text"),
    ("last_started_at", "timestamp with time zone"),
    ("last_finished_at", "timestamp with time zone"),
    ("last_success_at", "timestamp with time zone"),
    ("duration_seconds", "numeric"),
    ("is_overdue", "boolean"),
    ("message_code", "text"),
    ("checked_at", "timestamp with time zone"),
]

FORBIDDEN_OUTPUT_COLUMNS = [
    "job_name",
    "last_status",
    "last_sync_action",
    "last_warnings",
    "last_error",
    "fresh_event_count",
    "final_event_count",
    "github_run_id",
    "notification_status",
]


def extract_job_names_from_source(job_dir: Path) -> Set[str]:
    """Inspect Python AST of all job files in app/jobs/ to extract actual JobRunSpecification.job_name arguments."""
    from app.core import job_names as jn
    job_names = set()
    for py_file in job_dir.glob("*.py"):
        if py_file.name.startswith("__"):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "job_name":
                        if isinstance(keyword.value, ast.Constant):
                            job_names.add(keyword.value.value)
                        elif isinstance(keyword.value, ast.Name):
                            val = getattr(jn, keyword.value.id, None)
                            if val:
                                job_names.add(val)
    return job_names


def extract_catalog_job_names_from_sql(sql_content: str) -> Set[str]:
    """Extract all job_name strings from the SQL job_catalog CTE VALUES block."""
    matches = re.findall(r"\(\s*'([a-z0-9_]+)'::text\s*,\s*'([A-Z0-9_]+)'::text", sql_content)
    return {m[0] for m in matches}


def test_mobile_system_health_migration_timestamp_order():
    """Verify the migration timestamp is strictly after 20260829000000_add_market_summary_job_type.sql."""
    assert MIGRATION_FILE.exists(), f"Migration {MIGRATION_FILE} missing"
    assert MIGRATION_FILE.name > "20260829000000_add_market_summary_job_type.sql"


def test_mobile_system_health_returns_table_exact_13_columns():
    """Verify RETURNS TABLE contains exactly the 13 required columns in exact order, with no forbidden columns."""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")

    table_match = re.search(r"RETURNS\s+TABLE\s*\((.*?)\)\s*LANGUAGE", sql, re.DOTALL | re.IGNORECASE)
    assert table_match is not None, "Could not locate RETURNS TABLE block"
    columns_block = table_match.group(1).strip()

    col_defs = [line.strip().rstrip(",") for line in columns_block.split("\n") if line.strip() and not line.strip().startswith("--")]
    assert len(col_defs) == 13, f"Expected exactly 13 columns, found {len(col_defs)}: {col_defs}"

    for idx, (expected_name, expected_type) in enumerate(EXPECTED_13_COLUMNS):
        col_def = col_defs[idx].lower()
        assert expected_name in col_def, f"Column {idx+1} expected name '{expected_name}', got '{col_def}'"
        assert expected_type in col_def, f"Column {idx+1} expected type '{expected_type}', got '{col_def}'"

    for forbidden in FORBIDDEN_OUTPUT_COLUMNS:
        assert not re.search(rf"\b{forbidden}\b", columns_block, re.IGNORECASE), f"Forbidden column '{forbidden}' found in RETURNS TABLE"


def test_job_names_match_ast_extractions_exact_equality():
    """Verify that all 10 jobs in Python AST exactly equal the 10 catalog entries in SQL."""
    ast_jobs = extract_job_names_from_source(JOBS_DIR)
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    sql_catalog_jobs = extract_catalog_job_names_from_sql(sql)

    assert len(ast_jobs) == 10, f"Expected 10 production jobs in app/jobs/, found {len(ast_jobs)}: {ast_jobs}"
    assert len(sql_catalog_jobs) == 10, f"Expected 10 catalog jobs in SQL, found {len(sql_catalog_jobs)}: {sql_catalog_jobs}"
    assert ast_jobs == sql_catalog_jobs, f"Mismatch between AST jobs {ast_jobs} and SQL catalog jobs {sql_catalog_jobs}"


def test_mobile_system_health_syntax_and_security_guarantees():
    """Verify PostgreSQL syntax validity (no schema-qualified EXTRACT/GREATEST), ownership, and permissions."""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")

    # 1. Ensure schema-qualified EXTRACT or GREATEST are NOT present
    assert "pg_catalog.extract" not in sql, "pg_catalog.extract is invalid PostgreSQL syntax"
    assert "pg_catalog.greatest" not in sql, "pg_catalog.greatest is invalid PostgreSQL syntax"

    # 2. Ensure standard valid EXTRACT and GREATEST syntax are present
    assert re.search(r"EXTRACT\s*\(\s*EPOCH\s+FROM", sql, re.IGNORECASE) is not None
    assert re.search(r"GREATEST\s*\(", sql, re.IGNORECASE) is not None

    # 3. SECURITY DEFINER and search_path
    assert re.search(r"\bSECURITY\s+DEFINER\b", sql, re.IGNORECASE) is not None
    assert re.search(r"\bSET\s+search_path\s*=\s*pg_catalog\b", sql, re.IGNORECASE) is not None

    # 4. Ownership
    assert "ALTER FUNCTION public.get_mobile_system_health() OWNER TO postgres;" in sql

    # 5. Auth check
    assert "auth.uid() IS NULL" in sql
    assert "'authentication_required'" in sql

    # 6. Permissions
    assert "REVOKE ALL ON FUNCTION public.get_mobile_system_health() FROM PUBLIC;" in sql
    assert "REVOKE ALL ON FUNCTION public.get_mobile_system_health() FROM anon;" in sql
    assert "GRANT EXECUTE ON FUNCTION public.get_mobile_system_health() TO authenticated;" in sql

    # 7. Table isolation
    assert "REVOKE ALL PRIVILEGES ON TABLE public.refresh_runs FROM anon, authenticated;" in sql
    assert "REVOKE ALL PRIVILEGES ON TABLE public.provider_run_results FROM anon, authenticated;" in sql

    # 8. No dynamic SQL
    assert not re.search(r"\bEXECUTE\s+'", sql, re.IGNORECASE)


def test_sql_acceptance_test_file_rigor():
    """Verify SQL test file contains role switching, error catching, and discriminative test blocks."""
    assert SQL_TEST_FILE.exists()
    content = SQL_TEST_FILE.read_text(encoding="utf-8")

    assert "SET LOCAL ROLE anon;" in content
    assert "SET LOCAL ROLE authenticated;" in content
    assert "RESET ROLE;" in content
    assert "insufficient_privilege" in content
    assert "authentication_required" in content
    assert "aclexplode" in content
    assert "ORDER BY a.attnum" in content
    assert "ROLLBACK;" in content
