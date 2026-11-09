"""The archive command-line interface (src/archive_cli.py), with real temporary archives."""
import json
import subprocess
import sys
from datetime import timedelta

import pytest
import yaml

from src import archive_cli
from src.testing.archive import ResultArchive
from src.utils.config import PROJECT_ROOT
from test_archive_queries import START, archive_with, run_id, variant


@pytest.fixture
def archive(tmp_path):
    return archive_with(
        tmp_path,
        variant(tmp_path, 1, started=START),
        variant(tmp_path, 2, ammeter="entes", started=START + timedelta(minutes=1), outcomes=(4.0, 6.0)),
    )


def run_cli(capsys, *argv):
    code = archive_cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def usage_error(capsys, *argv):
    with pytest.raises(SystemExit) as excinfo:
        archive_cli.main(list(argv))
    _, err = capsys.readouterr()
    return excinfo.value.code, err


def write_config(tmp_path, result_management):
    path = tmp_path / "cli-config.yaml"
    data = {} if result_management is None else {"result_management": result_management}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --- Database selection ------------------------------------------------------------------

def test_explicit_db_works_without_reading_the_configuration(capsys, archive, monkeypatch):
    def no_config(*args, **kwargs):
        raise AssertionError("the configuration must not be read when --db is given")

    monkeypatch.setattr(archive_cli, "load_config", no_config)
    monkeypatch.chdir(archive.path.parent)  # a relative --db is resolved against the working directory

    code, out, err = run_cli(capsys, "--db", archive.path.name, "list")

    assert (code, err) == (0, "")
    assert run_id(1) in out and run_id(2) in out


def test_configured_database_is_used_even_when_archiving_is_disabled(capsys, tmp_path, archive):
    config = write_config(tmp_path, {"archive_enabled": False, "database_path": str(archive.path)})

    code, out, _ = run_cli(capsys, "--config", str(config), "show", run_id(1))

    assert code == 0 and json.loads(out)["run_id"] == run_id(1)


@pytest.mark.parametrize("section, message", [
    (None, "no archive database is configured"),
    ({"archive_enabled": False}, "no archive database is configured"),
    ({"archive_enabled": "yes"}, "archive_enabled must be true or false"),
])
def test_missing_or_invalid_configuration_is_an_operational_error(capsys, tmp_path, section, message):
    config = write_config(tmp_path, section)

    code, out, err = run_cli(capsys, "--config", str(config), "list")

    assert code == 1 and out == ""
    assert err.startswith("error: ") and message in err and "Traceback" not in err


def test_relative_configured_path_keeps_project_root_semantics(tmp_path, monkeypatch):
    config = write_config(tmp_path, {"archive_enabled": True, "database_path": "results/x.sqlite3"})
    monkeypatch.chdir(tmp_path)

    args = archive_cli.build_parser().parse_args(["--config", str(config), "list"])

    assert archive_cli._database_path(args) == PROJECT_ROOT / "results" / "x.sqlite3"


# --- Commands ----------------------------------------------------------------------------

def test_list_prints_a_compact_table(capsys, archive):
    code, out, _ = run_cli(capsys, "--db", str(archive.path), "list")

    lines = out.splitlines()
    assert code == 0 and lines[0].split()[:3] == ["STARTED", "(UTC)", "RUN"]
    assert [line.split()[2] for line in lines[1:]] == [run_id(2), run_id(1)]  # newest first
    assert "entes" in lines[1] and "2/2" in lines[1] and "count_reached" in lines[1] and " 5 " in f"{lines[1]} "


def test_list_filters_and_reports_no_matches(capsys, archive):
    code, out, _ = run_cli(capsys, "--db", str(archive.path), "list", "--ammeter", "entes", "--limit", "5")
    assert code == 0 and run_id(2) in out and run_id(1) not in out

    code, out, _ = run_cli(capsys, "--db", str(archive.path), "list",
                           "--since", "2026-11-08T19:31:00.654321+02:00")  # START + 1 min, in UTC+2
    assert code == 0 and run_id(2) in out and run_id(1) not in out

    code, out, _ = run_cli(capsys, "--db", str(archive.path), "list", "--until", "2026-11-08T17:30:00.654321Z")
    assert (code, out.strip()) == (0, "No archived runs match.")


def test_show_prints_the_complete_run_as_strict_json(capsys, archive):
    code, out, _ = run_cli(capsys, "--db", str(archive.path), "show", run_id(2))

    assert code == 0
    assert json.loads(out) == archive.load_run(run_id(2))


def test_compare_prints_json(capsys, archive):
    code, out, _ = run_cli(capsys, "--db", str(archive.path), "compare", run_id(1), run_id(2))

    comparison = json.loads(out)
    assert code == 0 and comparison["context_differences"] == ["ammeter"]
    assert comparison["metrics"]["mean_a"] == {"a": 1.5, "b": 5.0, "difference_b_minus_a": 3.5}


def test_export_writes_relative_to_the_working_directory_and_never_overwrites(capsys, archive, tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.chdir(out_dir)

    code, out, _ = run_cli(capsys, "--db", str(archive.path), "export", run_id(1), "run1.json")
    assert code == 0 and "Exported run" in out
    assert json.loads((out_dir / "run1.json").read_text(encoding="utf-8")) == archive.load_run(run_id(1))
    original = (out_dir / "run1.json").read_bytes()

    code, out, err = run_cli(capsys, "--db", str(archive.path), "export", run_id(2), "run1.json")
    assert code == 1 and "already exists" in err
    assert (out_dir / "run1.json").read_bytes() == original


# --- Errors and exit codes ---------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["show", run_id(9)],
    ["compare", run_id(1), run_id(9)],
    ["export", run_id(9), "never.json"],
])
def test_missing_run_is_an_operational_error(capsys, archive, monkeypatch, argv):
    monkeypatch.chdir(archive.path.parent)
    code, out, err = run_cli(capsys, "--db", str(archive.path), *argv)

    assert code == 1 and out == ""
    assert err == f"error: run {run_id(9)} is not archived in {archive.path}\n"
    assert not (archive.path.parent / "never.json").exists()


@pytest.mark.parametrize("command", [["list"], ["show", run_id(1)], ["export", run_id(1), "x.json"]])
def test_missing_database_is_reported_and_not_created(capsys, tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)

    code, _, err = run_cli(capsys, "--db", "missing.sqlite3", *command)

    assert code == 1 and err == "error: archive missing.sqlite3 does not exist\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_invalid_archive_is_an_operational_error(capsys, tmp_path):
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not a database" * 100)

    code, _, err = run_cli(capsys, "--db", str(bad), "list")

    assert code == 1 and "is not a valid SQLite database" in err and "Traceback" not in err


@pytest.mark.parametrize("argv, message", [
    (["show", "not-a-uuid"], "run_id must be a UUID"),
    (["show", run_id(0xabc).upper()], "canonical lowercase"),
    (["compare", run_id(1), "x"], "run_id must be a UUID"),
    (["export", "x", "out.json"], "run_id must be a UUID"),
    (["list", "--since", "2026-11-08T17:00:00"], "needs a UTC offset"),
    (["list", "--until", "yesterday"], "not an ISO 8601 timestamp"),
    (["list", "--limit", "0"], "must be from 1 to 1000"),
    (["list", "--limit", "1001"], "must be from 1 to 1000"),
    (["list", "--limit", "ten"], "not a whole number"),
    (["list", "--wrong"], "unrecognized arguments"),
    (["list", "--ammeter", ""], "argument --ammeter: must be a non-empty device name"),
    ([], "required: COMMAND"),
    (["--db", "a.sqlite3", "--config", "c.yaml", "list"], "not allowed with argument"),
])
def test_invalid_usage_exits_with_status_2(capsys, tmp_path, monkeypatch, argv, message):
    monkeypatch.chdir(tmp_path)

    code, err = usage_error(capsys, "--db", "never.sqlite3", *argv) if argv[:1] != ["--db"] \
        else usage_error(capsys, *argv)

    assert code == 2 and message in err
    assert list(tmp_path.iterdir()) == []


def test_since_not_before_until_is_a_usage_error(capsys, archive):
    code, _, err = run_cli(capsys, "--db", str(archive.path), "list",
                           "--since", "2026-11-08T18:00:00Z", "--until", "2026-11-08T19:00:00+01:00")

    assert code == 2 and err == "error: --since must be earlier than --until\n"


def test_unexpected_errors_are_not_hidden(archive, monkeypatch):
    def broken(self, **kwargs):
        raise RuntimeError("programming error")

    monkeypatch.setattr(ResultArchive, "list_runs", broken)

    with pytest.raises(RuntimeError, match="programming error"):
        archive_cli.main(["--db", str(archive.path), "list"])


def test_module_runs_as_a_separate_process(archive, tmp_path):
    def run(*argv):
        return subprocess.run([sys.executable, "-m", "src.archive_cli", *argv], cwd=PROJECT_ROOT,
                              capture_output=True, text=True, timeout=60)

    listed = run("--db", str(archive.path), "list")
    missing = run("--db", str(tmp_path / "missing.sqlite3"), "show", run_id(1))
    usage = run("--db", str(archive.path), "show", "bad")

    assert listed.returncode == 0 and run_id(1) in listed.stdout
    assert missing.returncode == 1 and missing.stderr.startswith("error: archive") and "Traceback" not in missing.stderr
    assert usage.returncode == 2 and "run_id must be a UUID" in usage.stderr
    assert not (tmp_path / "missing.sqlite3").exists()
