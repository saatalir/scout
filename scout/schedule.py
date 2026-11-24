from __future__ import annotations

import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

LABEL_PREFIX = "local.online-scout."
CRON_MARK = "# online-scout:"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
_DURATION_RE = re.compile(r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?$")


class ScheduleError(Exception):
    """Raised when a scheduler operation cannot be completed."""


def parse_time(text: str) -> tuple[int, int]:
    """Parse 'H:MM' or 'HH:MM' into (hour, minute)."""
    if not isinstance(text, str):
        raise ValueError(f"invalid time {text!r}: expected HH:MM")

    match = _TIME_RE.fullmatch(text)
    if not match:
        raise ValueError(f"invalid time {text!r}: expected HH:MM")

    hour_text, minute_text = text.split(":", 1)
    hour = int(hour_text)
    minute = int(minute_text)

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"invalid time {text!r}: expected HH:MM")

    return hour, minute


def parse_duration(text: str) -> int:
    """Parse a session length in minutes, e.g. '2h', '90m', '1h30m', '45'."""
    if not isinstance(text, str):
        raise ValueError(
            f"invalid duration {text!r}: expected forms like 2h, 90m, 1h30m"
        )

    value = text.strip().lower()
    if not value:
        raise ValueError(
            f"invalid duration {text!r}: expected forms like 2h, 90m, 1h30m"
        )

    minutes: int | None = None

    match = _DURATION_RE.fullmatch(value)
    if match and (match.group("h") is not None or match.group("m") is not None):
        hours = int(match.group("h") or 0)
        mins = int(match.group("m") or 0)
        minutes = hours * 60 + mins
    elif value.isdigit():
        minutes = int(value)

    if minutes is None:
        raise ValueError(
            f"invalid duration {text!r}: expected forms like 2h, 90m, 1h30m"
        )

    if minutes <= 0 or minutes > 1440:
        raise ValueError(
            f"invalid duration {text!r}: expected forms like 2h, 90m, 1h30m"
        )

    return minutes


def check_task_id(task_id: str) -> str:
    """Return task_id if it is safe for labels, plist names and cron markers."""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError(
            f"invalid task id {task_id!r}: no spaces, quotes, ;, $, / ..."
        )
    return task_id


@dataclass
class Job:
    task_id: str
    hour: int
    minute: int
    command: list[str]
    where: str


def command_for(task_id: str, session: str, python: str) -> list[str]:
    """Build the argv for an unattended Scout resume session."""
    task_id = check_task_id(task_id)
    parse_duration(session)
    return [
        python,
        "-m",
        "scout",
        "resume",
        task_id,
        "--session",
        session,
        "--unattended",
    ]


def _default_run(args: list[str], input: str | None = None) -> Any:
    return subprocess.run(args, input=input, capture_output=True, text=True, check=False)


def _default_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return 0
    return int(getuid())


class Scheduler:
    def __init__(
        self,
        *,
        system: str | None = None,
        home: Path | None = None,
        run: Callable[..., Any] | None = None,
        python: str | None = None,
        root: Path | None = None,
        log_dir: Callable[[str], Path] | None = None,
        uid: int | None = None,
        path_env: str | None = None,
    ) -> None:
        self.system = system if system is not None else platform.system()
        self.home = Path(home) if home is not None else Path.home()
        self.run = run if run is not None else _default_run
        self.python = python if python is not None else sys.executable
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.uid = uid if uid is not None else _default_uid()
        self.path_env = path_env if path_env is not None else os.environ.get("PATH", "")

        if log_dir is None:
            root = self.root

            def _default_log_dir(task_id: str) -> Path:
                return root / "tasks" / task_id

            self.log_dir = _default_log_dir
        else:
            self.log_dir = log_dir

    def install(self, task_id: str, hour: int, minute: int, session: str) -> Job:
        self._check_system()
        task_id = check_task_id(task_id)
        command = command_for(task_id, session, self.python)
        hour, minute = self._check_time(hour, minute)

        if self.system == "Darwin":
            return self._install_macos(task_id, hour, minute, command)

        return self._install_linux(task_id, hour, minute, command)

    def remove(self, task_id: str) -> bool:
        self._check_system()
        task_id = check_task_id(task_id)

        if self.system == "Darwin":
            return self._remove_macos(task_id)

        return self._remove_linux(task_id)

    def list(self) -> list[Job]:
        self._check_system()

        if self.system == "Darwin":
            return self._list_macos()

        return self._list_linux()

    def _check_system(self) -> None:
        if self.system not in {"Darwin", "Linux"}:
            raise ScheduleError(
                f"unsupported system {self.system!r}: schedules need macOS (launchd) "
                "or Linux (cron)"
            )

    def _check_time(self, hour: int, minute: int) -> tuple[int, int]:
        try:
            hour_int = int(hour)
            minute_int = int(minute)
        except (TypeError, ValueError) as exc:
            raise ValueError("hour and minute must be integers") from exc

        if not 0 <= hour_int <= 23:
            raise ValueError(f"invalid hour {hour!r}: expected 0-23")
        if not 0 <= minute_int <= 59:
            raise ValueError(f"invalid minute {minute!r}: expected 0-59")

        return hour_int, minute_int

    def _label(self, task_id: str) -> str:
        return LABEL_PREFIX + task_id

    def _plist_path(self, task_id: str) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self._label(task_id)}.plist"

    def _launch_agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def _install_macos(self, task_id: str, hour: int, minute: int, command: list[str]) -> Job:
        label = self._label(task_id)
        plist_path = self._plist_path(task_id)
        agents_dir = plist_path.parent
        agents_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.log_dir(task_id) / "schedule.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if plist_path.exists():
            self.run(["launchctl", "bootout", f"gui/{self.uid}/{label}"])

        data = {
            "Label": label,
            "ProgramArguments": list(command),
            "WorkingDirectory": str(self.root),
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "EnvironmentVariables": {"PATH": self.path_env},
            "RunAtLoad": False,
        }

        tmp_path = plist_path.with_name(plist_path.name + ".tmp")
        with tmp_path.open("wb") as handle:
            plistlib.dump(data, handle)
        tmp_path.replace(plist_path)

        result = self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(plist_path)])
        if getattr(result, "returncode", 0) != 0:
            stderr = getattr(result, "stderr", "") or ""
            plist_path.unlink(missing_ok=True)
            raise ScheduleError(
                f"launchctl bootstrap failed (exit {getattr(result, 'returncode', 1)}): {stderr}"
            )

        return Job(
            task_id=task_id,
            hour=hour,
            minute=minute,
            command=list(command),
            where=str(plist_path),
        )

    def _remove_macos(self, task_id: str) -> bool:
        label = self._label(task_id)
        plist_path = self._plist_path(task_id)

        if not plist_path.exists():
            return False

        self.run(["launchctl", "bootout", f"gui/{self.uid}/{label}"])
        plist_path.unlink(missing_ok=True)
        return True

    def _list_macos(self) -> list[Job]:
        agents_dir = self._launch_agents_dir()
        if not agents_dir.is_dir():
            return []

        jobs: list[Job] = []

        for path in agents_dir.glob(f"{LABEL_PREFIX}*.plist"):
            task_id = path.name[len(LABEL_PREFIX) : -len(".plist")]
            if not task_id or not TASK_ID_RE.fullmatch(task_id):
                continue

            try:
                with path.open("rb") as handle:
                    data = plistlib.load(handle)

                interval = data["StartCalendarInterval"]
                args = data["ProgramArguments"]
                if not isinstance(args, list):
                    continue

                jobs.append(
                    Job(
                        task_id=task_id,
                        hour=int(interval["Hour"]),
                        minute=int(interval["Minute"]),
                        command=list(args),
                        where=str(path),
                    )
                )
            except Exception:
                continue

        jobs.sort(key=lambda job: job.task_id)
        return jobs

    def _read_crontab(self) -> str:
        result = self.run(["crontab", "-l"])
        returncode = getattr(result, "returncode", 0)

        if returncode == 0:
            return getattr(result, "stdout", "") or ""

        stderr = getattr(result, "stderr", "") or ""
        if "no crontab" in stderr.lower():
            return ""

        raise ScheduleError(f"crontab failed (exit {returncode}): {stderr}")

    def _cron_line(self, task_id: str, hour: int, minute: int, command: list[str]) -> str:
        log_path = self.log_dir(task_id) / "schedule.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        q = shlex.quote
        joined_command = " ".join(q(arg) for arg in command)

        return (
            f"{minute} {hour} * * * cd {q(str(self.root))} && {joined_command} "
            f">> {q(str(log_path))} 2>&1 {CRON_MARK}{task_id}"
        )

    def _install_linux(self, task_id: str, hour: int, minute: int, command: list[str]) -> Job:
        current = self._read_crontab()
        marker = CRON_MARK + task_id
        lines = current.splitlines()
        kept = [line for line in lines if not line.rstrip().endswith(marker)]
        new_line = self._cron_line(task_id, hour, minute, command)
        new_text = "\n".join(kept + [new_line]) + "\n"

        result = self.run(["crontab", "-"], input=new_text)
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise ScheduleError(f"crontab failed (exit {returncode}): {stderr}")

        return Job(
            task_id=task_id,
            hour=hour,
            minute=minute,
            command=list(command),
            where="crontab",
        )

    def _remove_linux(self, task_id: str) -> bool:
        current = self._read_crontab()
        marker = CRON_MARK + task_id
        lines = current.splitlines()
        kept = [line for line in lines if not line.rstrip().endswith(marker)]

        if len(kept) == len(lines):
            return False

        new_text = "\n".join(kept) + ("\n" if kept else "")
        result = self.run(["crontab", "-"], input=new_text)
        returncode = getattr(result, "returncode", 0)
        if returncode != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise ScheduleError(f"crontab failed (exit {returncode}): {stderr}")

        return True

    def _list_linux(self) -> list[Job]:
        current = self._read_crontab()
        jobs: list[Job] = []

        for raw_line in current.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            marker_index = line.rfind(CRON_MARK)
            if marker_index == -1:
                continue

            task_id = line[marker_index + len(CRON_MARK) :].strip()
            if not task_id or not TASK_ID_RE.fullmatch(task_id):
                continue

            try:
                tokens = shlex.split(line)
            except ValueError:
                continue

            if len(tokens) < 6:
                continue

            try:
                minute = int(tokens[0])
                hour = int(tokens[1])
            except ValueError:
                continue

            try:
                and_index = tokens.index("&&")
                redirect_index = tokens.index(">>", and_index + 1)
            except ValueError:
                continue

            command = tokens[and_index + 1 : redirect_index]
            if not command:
                continue

            jobs.append(
                Job(
                    task_id=task_id,
                    hour=hour,
                    minute=minute,
                    command=command,
                    where="crontab",
                )
            )

        jobs.sort(key=lambda job: job.task_id)
        return jobs
