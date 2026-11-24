---
files: ["scout/schedule.py", "tests/test_schedule.py"]
context: ["docs/RIGOR.md"]
tests: ["tests/test_schedule.py"]
---
# RG10: install, list and remove daily schedules for research campaigns

A research campaign (docs/RIGOR.md, "Long-running research") is one Scout task that runs in sessions. The
user can ask for a session every day at a fixed time. This package writes the **scheduler adapter only**:
a per-user launchd agent on macOS, a crontab line on Linux. Write a NEW module `scout/schedule.py`
(`from __future__ import annotations`, standard library only, no network) and its tests. Do not change any
other file; the CLI that calls this module is written separately.

**What must never happen:**
- Tests must **never** run the real `launchctl` or `crontab`, and never write under the real home folder.
  Every system call goes through the injected `run` callable; every path is under the injected `home`.
- A task id or session string must never be able to inject shell text into a crontab line or a plist.
  Validate them (see below) and quote every argument with `shlex.quote` in the cron line.
- `install` must never leave two jobs for the same task (re-installing replaces the old one).
- `remove` must never touch another task's job or any line/plist that Scout did not create (only lines
  ending with the marker for that id; only plists whose name starts with the label prefix).
- Removing a job that does not exist is not an error: it returns False.

## API

```python
LABEL_PREFIX = "local.online-scout."      # launchd label: LABEL_PREFIX + task_id
CRON_MARK = "# online-scout:"             # crontab line ends with CRON_MARK + task_id
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ScheduleError(Exception):
    """Carries the cause and the fix, e.g. "unsupported system 'Windows': schedules need macOS (launchd) or
    Linux (cron)" or "crontab failed (exit 1): <stderr>"."""


def parse_time(text: str) -> tuple[int, int]:
    """'09:00' -> (9, 0); '7:05' -> (7, 5). Hours 0-23, minutes 0-59, exactly H:MM or HH:MM.
    Anything else raises ValueError naming the expected format (HH:MM)."""


def parse_duration(text: str) -> int:
    """A session length in minutes: '2h' -> 120, '90m' -> 90, '1h30m' -> 90, '45' -> 45, ' 2H ' -> 120.
    Must be > 0 and <= 24 h (1440). Anything else ('', '0', '-5m', '2x', '25h') raises ValueError naming the
    accepted forms (like 2h, 90m, 1h30m)."""


def check_task_id(task_id: str) -> str:
    """Returns the id if it matches TASK_ID_RE, else raises ValueError (no spaces, quotes, ;, $, / ...)."""


@dataclass
class Job:
    task_id: str
    hour: int
    minute: int
    command: list[str]           # the argv that runs: [python, "-m", "scout", "resume", id, "--session", s, "--unattended"]
    where: str                   # the plist path (macOS) or "crontab" (Linux)


def command_for(task_id: str, session: str, python: str) -> list[str]:
    """[python, "-m", "scout", "resume", task_id, "--session", session, "--unattended"]. Validates the id
    and the session (parse_duration) first."""


class Scheduler:
    def __init__(self, *, system: str | None = None, home: Path | None = None,
                 run: Callable[..., Any] | None = None, python: str | None = None,
                 root: Path | None = None, log_dir: Callable[[str], Path] | None = None,
                 uid: int | None = None, path_env: str | None = None):
        """system: platform.system() by default ('Darwin' or 'Linux'; anything else -> ScheduleError on use).
        home: Path.home(). run(args: list[str], input: str | None = None) -> object with .returncode, .stdout,
        .stderr (default: subprocess.run with capture_output=True, text=True, check=False).
        python: sys.executable. root: the repository folder (working directory of the job; default the
        parent of the scout package). log_dir(task_id) -> folder for the job's log file `schedule.log`
        (default: root / "tasks" / task_id). uid: os.getuid(). path_env: os.environ PATH (put into the plist so
        ssh and the browsers are found)."""

    def install(self, task_id: str, hour: int, minute: int, session: str) -> Job: ...
    def remove(self, task_id: str) -> bool: ...
    def list(self) -> list[Job]: ...
```

### macOS (launchd)

- Plist path: `home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{task_id}.plist"` (create the folder).
- Written with `plistlib`, keys: `Label`, `ProgramArguments` (= `command_for(...)`), `WorkingDirectory`
  (str(root)), `StartCalendarInterval` = `{"Hour": hour, "Minute": minute}`, `StandardOutPath` and
  `StandardErrorPath` (both `log_dir(task_id) / "schedule.log"`; create that folder), `EnvironmentVariables` =
  `{"PATH": path_env}`, `RunAtLoad` = False.
- install: if the plist already exists, first `run(["launchctl", "bootout", f"gui/{uid}/{label}"])` (its
  result is ignored); write the plist (atomically: write a `.tmp` next to it, then replace); then
  `run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])`. A non-zero return code raises
  ScheduleError with the stderr, after deleting the plist it just wrote.
- remove: no plist -> False. Else `run(["launchctl", "bootout", f"gui/{uid}/{label}"])` (result ignored:
  it may not be loaded), delete the plist, return True.
- list: every `LABEL_PREFIX*.plist` in the LaunchAgents folder, read with plistlib, into Jobs (sorted by
  task id). A plist that cannot be read is skipped.

### Linux (cron)

- Read: `run(["crontab", "-l"])`. Return code 0 -> its stdout. Non-zero with "no crontab" in stderr (any
  case) -> empty. Any other non-zero -> ScheduleError.
- The line: `f"{minute} {hour} * * * cd {q(root)} && {' '.join(q(a) for a in command)} >> {q(log)} 2>&1 {CRON_MARK}{task_id}"`
  where `q = shlex.quote` and `log = log_dir(task_id) / "schedule.log"` (create the folder).
- install: drop every existing line that ends with `CRON_MARK + task_id` (exactly that id: a line for
  `abc-1` must not match `abc`), append the new line, keep every other line as it was, end with a newline,
  write with `run(["crontab", "-"], input=text)`. Non-zero -> ScheduleError.
- remove: no such line -> False (and do not write). Else write the crontab without it -> True.
- list: parse Scout's lines (ending with the marker): minute, hour from the first two fields; command:
  `shlex.split` of the part between `&& ` and ` >> `. Sorted by task id.

## Tests (`tests/test_schedule.py`)

Offline, plain pytest, with a fake runner that records calls and holds a fake crontab:

```python
class FakeRun:
    def __init__(self, crontab=None, fail=None):
        self.calls, self.crontab, self.fail = [], crontab, fail or {}   # fail: {"bootstrap": (1, "boom")}
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
```

At least:
1. parse_time and parse_duration: the examples above, valid and invalid (parametrized).
2. check_task_id rejects `"a b"`, `"a;rm -rf ~"`, `"$(x)"`, `"../x"`, `""`, and accepts
   `"20261124-122016-what-is-known-about-storing-hydr-965d"`.
3. macOS install (tmp_path as home and root, system="Darwin", uid=501): the plist exists with the right
   keys (read it back with plistlib), `ProgramArguments[-4:] == ["resume", id, "--session", "2h"]` plus the
   trailing "--unattended", the bootstrap call is `["launchctl", "bootstrap", "gui/501", <path>]`, and no
   bootout was called on a first install.
4. macOS re-install with another time: one plist, the new hour, a bootout before the bootstrap.
5. macOS bootstrap failure: ScheduleError mentioning the stderr, and no plist left.
6. macOS remove: True and the plist is gone, a bootout was called; a second remove: False. A foreign plist
   (`com.example.other.plist`) in the same folder is never listed or removed.
7. Linux install into an empty crontab (None) and into one with two foreign lines: the foreign lines stay
   unchanged and in order; Scout's line ends with the marker; the command part `shlex.split`s back to
   `command_for(...)`; `list()` returns the job with the right hour and minute.
8. Linux: install for `abc-1` and `abc`, remove `abc`: the `abc-1` line stays.
9. Linux re-install replaces (one line per id); remove of a missing id returns False and does not write.
10. Linux `crontab -l` failing with another error: ScheduleError.
11. A root path with a space and a quote in it (`tmp_path / "my dir's"`): the cron line still
    `shlex.split`s into the expected words (quoting works).
12. An unsupported system ("Windows"): install raises ScheduleError naming macOS and Linux.
13. `command_for` with a bad id or a bad session raises ValueError before anything is written.
