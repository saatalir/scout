from __future__ import annotations

import plistlib
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout.schedule import (
    CRON_MARK,
    LABEL_PREFIX,
    Job,
    ScheduleError,
    Scheduler,
    check_task_id,
    command_for,
    parse_duration,
    parse_time,
)


class FakeRun:
    def __init__(self, crontab=None, fail=None):
        self.calls, self.crontab, self.fail = [], crontab, fail or {}

    def __call__(self, args, input=None):
        self.calls.append((list(args), input))
        name = args[1] if args[0] == "launchctl" else args[0]

        if name in self.fail:
            code, err = self.fail[name]
            return SimpleNamespace(returncode=code, stdout="", stderr=err)

        if args[:2] == ["crontab", "-l"]:
            if self.crontab is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="no crontab for user")
            return SimpleNamespace(returncode=0, stdout=self.crontab, stderr="")

        if args[:2] == ["crontab", "-"]:
            self.crontab = input

        return SimpleNamespace(returncode=0, stdout="", stderr="")


def make_scheduler(
    tmp_path: Path,
    *,
    system: str = "Linux",
    crontab=None,
    fail=None,
    uid: int = 501,
    python: str = "/usr/bin/python",
    path_env: str = "/usr/bin:/bin",
    root: Path | None = None,
) -> tuple[Scheduler, FakeRun]:
    fake = FakeRun(crontab=crontab, fail=fail)
    scheduler = Scheduler(
        system=system,
        home=tmp_path / "home",
        root=root or (tmp_path / "repo"),
        run=fake,
        python=python,
        uid=uid,
        path_env=path_env,
    )
    return scheduler, fake


def command_from_line(line: str) -> list[str]:
    tokens = shlex.split(line)
    and_index = tokens.index("&&")
    redirect_index = tokens.index(">>", and_index + 1)
    return tokens[and_index + 1 : redirect_index]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09:00", (9, 0)),
        ("7:05", (7, 5)),
        ("0:00", (0, 0)),
        ("00:00", (0, 0)),
        ("23:59", (23, 59)),
    ],
)
def test_parse_time_valid(text: str, expected: tuple[int, int]) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "9:0",
        "09:60",
        "24:00",
        "23:60",
        "000:00",
        "0:000",
        " 09:00",
        "09:00 ",
        "a",
        "09:00:00",
    ],
)
def test_parse_time_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2h", 120),
        ("90m", 90),
        ("1h30m", 90),
        ("45", 45),
        (" 2H ", 120),
        ("24h", 1440),
        ("1440m", 1440),
        ("0h30m", 30),
        ("1h0m", 60),
    ],
)
def test_parse_duration_valid(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "0",
        "-5m",
        "2x",
        "25h",
        "1441m",
        "1h30",
        "1h 30m",
        "0h0m",
        "1h1440m",
    ],
)
def test_parse_duration_invalid(text: str) -> None:
    with pytest.raises(ValueError, match="2h, 90m, 1h30m"):
        parse_duration(text)


@pytest.mark.parametrize(
    "task_id",
    [
        "a b",
        "a;rm -rf ~",
        "$(x)",
        "../x",
        "",
        "-x",
        ".x",
        "x/y",
        "x'y",
    ],
)
def test_check_task_id_rejects(task_id: str) -> None:
    with pytest.raises(ValueError):
        check_task_id(task_id)


def test_check_task_id_accepts_long_id() -> None:
    task_id = "20261124-122016-what-is-known-about-storing-hydr-965d"
    assert check_task_id(task_id) == task_id


def test_command_for() -> None:
    assert command_for("abc", "2h", "/usr/bin/python") == [
        "/usr/bin/python",
        "-m",
        "scout",
        "resume",
        "abc",
        "--session",
        "2h",
        "--unattended",
    ]


def test_macos_install_first(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Darwin")
    task_id = "abc"
    job = scheduler.install(task_id, 9, 5, "2h")

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{task_id}.plist"
    assert plist_path.exists()
    assert job.where == str(plist_path)

    with plist_path.open("rb") as handle:
        data = plistlib.load(handle)

    log_path = tmp_path / "repo" / "tasks" / task_id / "schedule.log"
    assert data["Label"] == LABEL_PREFIX + task_id
    assert data["ProgramArguments"] == command_for(task_id, "2h", "/usr/bin/python")
    assert data["ProgramArguments"][-5:] == [
        "resume",
        task_id,
        "--session",
        "2h",
        "--unattended",
    ]
    assert data["WorkingDirectory"] == str(tmp_path / "repo")
    assert data["StartCalendarInterval"] == {"Hour": 9, "Minute": 5}
    assert data["StandardOutPath"] == str(log_path)
    assert data["StandardErrorPath"] == str(log_path)
    assert data["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    assert data["RunAtLoad"] is False

    launch_calls = [args for args, _ in fake.calls if args[0] == "launchctl"]
    assert launch_calls == [["launchctl", "bootstrap", "gui/501", str(plist_path)]]
    assert not any(args[1] == "bootout" for args in launch_calls)


def test_macos_reinstall_replaces(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Darwin")
    task_id = "abc"
    scheduler.install(task_id, 9, 5, "2h")
    scheduler.install(task_id, 10, 15, "2h")

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{task_id}.plist"
    plists = list((tmp_path / "home" / "Library" / "LaunchAgents").glob("*.plist"))
    assert plists == [plist_path]

    with plist_path.open("rb") as handle:
        data = plistlib.load(handle)
    assert data["StartCalendarInterval"] == {"Hour": 10, "Minute": 15}

    launch_calls = [args for args, _ in fake.calls if args[0] == "launchctl"]
    assert [args[1] for args in launch_calls] == ["bootstrap", "bootout", "bootstrap"]
    assert launch_calls[1] == ["launchctl", "bootout", f"gui/501/{LABEL_PREFIX}{task_id}"]
    assert launch_calls[2] == ["launchctl", "bootstrap", "gui/501", str(plist_path)]


def test_macos_bootstrap_failure(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Darwin", fail={"bootstrap": (1, "boom")})

    with pytest.raises(ScheduleError, match="boom"):
        scheduler.install("abc", 9, 5, "2h")

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}abc.plist"
    assert not plist_path.exists()


def test_macos_remove(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Darwin")
    task_id = "abc"
    scheduler.install(task_id, 9, 5, "2h")

    foreign = tmp_path / "home" / "Library" / "LaunchAgents" / "com.example.other.plist"
    with foreign.open("wb") as handle:
        plistlib.dump({"Label": "com.example.other"}, handle)

    assert scheduler.remove(task_id) is True

    plist_path = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{task_id}.plist"
    assert not plist_path.exists()
    assert foreign.exists()

    launch_calls = [args for args, _ in fake.calls if args[0] == "launchctl"]
    assert ["launchctl", "bootout", f"gui/501/{LABEL_PREFIX}{task_id}"] in launch_calls

    calls_before = len(fake.calls)
    assert scheduler.remove(task_id) is False
    assert len(fake.calls) == calls_before

    jobs = scheduler.list()
    assert [job.task_id for job in jobs] == []


def test_macos_list_skips_bad_plist(tmp_path: Path) -> None:
    scheduler, _ = make_scheduler(tmp_path, system="Darwin")
    scheduler.install("abc", 9, 5, "2h")

    bad = tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}bad.plist"
    bad.write_text("not a plist")

    jobs = scheduler.list()
    assert [job.task_id for job in jobs] == ["abc"]


def test_linux_install_empty(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=None)
    task_id = "abc"
    job = scheduler.install(task_id, 9, 5, "2h")

    assert job.where == "crontab"
    assert fake.crontab is not None
    lines = fake.crontab.splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(f"{CRON_MARK}{task_id}")
    assert command_from_line(lines[0]) == command_for(task_id, "2h", "/usr/bin/python")

    jobs = scheduler.list()
    assert len(jobs) == 1
    assert jobs[0].task_id == task_id
    assert jobs[0].hour == 9
    assert jobs[0].minute == 5


def test_linux_install_with_foreign_lines(tmp_path: Path) -> None:
    foreign = "0 * * * * /usr/bin/foreign1\n@reboot /usr/bin/foreign2\n"
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=foreign)
    task_id = "abc"
    scheduler.install(task_id, 9, 5, "2h")

    lines = fake.crontab.splitlines()
    assert len(lines) == 3
    assert lines[0] == "0 * * * * /usr/bin/foreign1"
    assert lines[1] == "@reboot /usr/bin/foreign2"
    assert lines[2].endswith(f"{CRON_MARK}{task_id}")


def test_linux_install_abc_1_and_abc(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=None)
    scheduler.install("abc-1", 9, 5, "2h")
    scheduler.install("abc", 10, 15, "2h")

    assert scheduler.remove("abc") is True
    lines = fake.crontab.splitlines()
    assert len(lines) == 1
    assert lines[0].endswith(f"{CRON_MARK}abc-1")


def test_linux_reinstall_replaces(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=None)
    scheduler.install("abc", 9, 5, "2h")
    scheduler.install("abc", 10, 15, "2h")

    lines = fake.crontab.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("15 10 * * *")


def test_linux_remove_missing(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=None)
    assert scheduler.remove("abc") is False
    assert not any(args[:2] == ["crontab", "-"] for args, _ in fake.calls)


def test_linux_crontab_l_error(tmp_path: Path) -> None:
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab="")
    fake.fail["crontab"] = (1, "some other error")

    with pytest.raises(ScheduleError, match="some other error"):
        scheduler.install("abc", 9, 5, "2h")


def test_linux_root_with_space_and_quote(tmp_path: Path) -> None:
    root = tmp_path / "my dir's"
    root.mkdir()
    scheduler, fake = make_scheduler(tmp_path, system="Linux", crontab=None, root=root)
    scheduler.install("abc", 9, 5, "2h")

    line = fake.crontab.splitlines()[0]
    assert shlex.quote(str(root)) in line

    cmd = command_from_line(line)
    assert cmd == command_for("abc", "2h", "/usr/bin/python")


def test_unsupported_system(tmp_path: Path) -> None:
    scheduler, _ = make_scheduler(tmp_path, system="Windows")
    with pytest.raises(ScheduleError, match="macOS"):
        scheduler.install("abc", 9, 5, "2h")
    with pytest.raises(ScheduleError, match="Linux"):
        scheduler.install("abc", 9, 5, "2h")


def test_command_for_bad_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        command_for("a b", "2h", "/usr/bin/python")


def test_command_for_bad_session(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        command_for("abc", "2x", "/usr/bin/python")
