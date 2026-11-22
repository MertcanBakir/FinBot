import ast
import builtins
import importlib
import inspect
from pathlib import Path
from typing import Set, get_type_hints

import pytest

JOBS_DIR = Path(__file__).parent.parent / "app" / "jobs"
APP_DIR = Path(__file__).parent.parent / "app"

PRODUCTION_JOB_MODULES = [
    "app.jobs.binance_crypto_latest_prices_job",
    "app.jobs.bist_latest_prices_job",
    "app.jobs.nasdaq_latest_prices_job",
    "app.jobs.tefas_latest_prices_job",
    "app.jobs.fx_latest_price_job",
    "app.jobs.portfolio_snapshots_job",
    "app.jobs.economic_calendar_refresh_job",
    "app.jobs.earnings_refresh_job",
    "app.jobs.morning_job",
    "app.jobs.closing_job",
]


def extract_scope_names(tree: ast.AST) -> Set[str]:
    """Collect all built-in, imported, or locally defined top-level names in an AST module."""
    names = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def find_undefined_annotations_in_file(file_path: Path) -> list[str]:
    """Statically verify that every type annotation identifier is resolvable in the module scope (preventing Python 3.11 NameError)."""
    code = file_path.read_text(encoding="utf-8")
    tree = ast.parse(code, filename=str(file_path))
    scope_names = extract_scope_names(tree)
    errors = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Check parameter annotations
            for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
                if arg.annotation:
                    for subnode in ast.walk(arg.annotation):
                        if isinstance(subnode, ast.Name) and subnode.id not in scope_names:
                            errors.append(f"{file_path.name}:{node.lineno} in arg '{arg.arg}': undefined '{subnode.id}'")
            # Check return annotation
            if node.returns:
                for subnode in ast.walk(node.returns):
                    if isinstance(subnode, ast.Name) and subnode.id not in scope_names:
                        errors.append(f"{file_path.name}:{node.lineno} in return: undefined '{subnode.id}'")

    return errors


@pytest.mark.parametrize("module_path", list(JOBS_DIR.glob("*.py")))
def test_job_files_ast_annotations_statically_defined(module_path: Path):
    """Ensure all job files have 100% defined annotations preventing definition-time NameError under Python 3.11."""
    if module_path.name.startswith("__"):
        return
    errors = find_undefined_annotations_in_file(module_path)
    assert not errors, f"Undefined type annotations detected in {module_path.name}:\n" + "\n".join(errors)


@pytest.mark.parametrize("mod_name", PRODUCTION_JOB_MODULES)
def test_all_10_production_jobs_import_and_type_hints_resolvable(mod_name: str):
    """Import each production job module and explicitly evaluate typing.get_type_hints on all functions and classes."""
    mod = importlib.import_module(mod_name)
    assert mod is not None

    # Evaluate type hints on module-level functions
    for _name, obj in inspect.getmembers(mod, inspect.isfunction):
        if obj.__module__ == mod_name:
            hints = get_type_hints(obj)
            assert isinstance(hints, dict)

    # Evaluate type hints on classes and their methods
    for _name, obj in inspect.getmembers(mod, inspect.isclass):
        if obj.__module__ == mod_name:
            hints = get_type_hints(obj)
            assert isinstance(hints, dict)
            for _m_name, m_obj in inspect.getmembers(obj, inspect.isfunction):
                if m_obj.__module__ == mod_name:
                    m_hints = get_type_hints(m_obj)
                    assert isinstance(m_hints, dict)
