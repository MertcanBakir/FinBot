import sys

from app.core.database import check_database_health


def run_health_check() -> int:
    """Run database health check CLI job.

    Returns exit code 0 on success, 1 on failure.
    Never prints credentials, hostnames, usernames, or sensitive connection strings.
    """
    result = check_database_health()

    if result.status == "success":
        print("Database connection: OK")
        print(
            f"Required tables: {result.found_table_count}/{result.expected_table_count} found"
        )
        print("Schema validation: PASSED")
        return 0

    if result.connected:
        print("Database connection: OK")
        if result.error_code == "missing_tables":
            print(
                f"Required tables: {result.found_table_count}/{result.expected_table_count} found"
            )
            if result.missing_tables:
                missing_str = ", ".join(result.missing_tables)
                print(f"Missing tables: {missing_str}")
        if result.error_code:
            print(f"Error code: {result.error_code}")
        if result.error:
            print(f"Error: {result.error}")
        print("Schema validation: FAILED")
        return 1

    # Not connected
    print("Database connection: FAILED")
    if result.error_code:
        print(f"Error code: {result.error_code}")
    if result.error:
        print(f"Error: {result.error}")
    print("Schema validation: FAILED")
    return 1


def main() -> None:
    sys.exit(run_health_check())


if __name__ == "__main__":
    main()
