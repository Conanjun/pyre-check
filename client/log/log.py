# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import contextlib
import copy
import io
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from types import TracebackType
from typing import Generator, Iterable, Optional, Pattern, Sequence

import click


PERFORMANCE: int = 15
PROMPT: int = 50
SUCCESS: int = 60


LOG: logging.Logger = logging.getLogger(__name__)


stdout: io.StringIO = io.StringIO(newline="")
__handler: Optional["TimedStreamHandler"] = None


class Color:
    YELLOW: str = "\033[33m"
    RED: str = "\033[31m"
    GREEN: str = "\033[32m"


class Format:
    BOLD: str = "\033[1m"

    CLEAR_LINE: str = "\x1b[0G\x1b[K"
    CLEAR: str = "\033[0m"
    TRUNCATE_OVERFLOW: str = "\033[?7l"
    WRAP_OVERFLOW: str = "\033[?7h"
    NEWLINE: str = "\n"

    CURSOR_UP_LINE: str = "\x1b[1A"
    HIDE_CURSOR: str = "\x1b[?25l"
    SHOW_CURSOR: str = "\x1b[?25h"


class Character:
    LAMBDA: str = "ƛ"


class SectionFormatter(logging.Formatter):
    def __init__(self) -> None:
        super(SectionFormatter, self).__init__(
            "%(asctime)s [PID %(process)d] %(levelname)s %(message)s"
        )

    def format(self, record: logging.LogRecord) -> str:
        formatted = super(SectionFormatter, self).format(record)
        return re.sub(r"DEBUG \[(.*)\]", r"\1", formatted)


class TimedStreamHandler(logging.StreamHandler):
    THRESHOLD: float = 0.5
    LINE_BREAKING_LEVELS: Sequence[str] = ["ERROR", "WARNING", "SUCCESS"]

    _terminate: bool = False
    _last_update: float = 0.0

    def __init__(self) -> None:
        super(TimedStreamHandler, self).__init__()
        self.setFormatter(logging.Formatter("%(message)s"))
        self.terminator: str = ""
        self.setLevel(logging.INFO)

        self._record: Optional[logging.LogRecord] = None
        self._active_lines: int = 0

        # Preamble preparing terminal.
        click.echo(
            Format.NEWLINE
            + Format.CLEAR_LINE
            + Format.CURSOR_UP_LINE
            + Format.HIDE_CURSOR,
            file=sys.stderr,
            nl=False,
        )

        thread = threading.Thread(target=self._thread)
        thread.daemon = True
        thread.start()

    def clear_lines(self) -> str:
        if self._active_lines == 0:
            return ""
        return Format.CLEAR_LINE + "".join(
            [
                Format.CURSOR_UP_LINE + Format.CLEAR_LINE
                for n in range(self._active_lines - 1)
            ]
        )

    def emit(self, record: logging.LogRecord, age: Optional[float] = None) -> None:
        suffix = ""
        color: Optional[str] = None
        message = record.msg
        active_lines = message.count("\n") + 1
        truncate = Format.TRUNCATE_OVERFLOW
        if record.levelname in self.LINE_BREAKING_LEVELS:
            message += "\n"

        if record.levelname == "ERROR":
            color = "red"
            self._record = None
            active_lines = 0
            truncate = Format.WRAP_OVERFLOW
        elif record.levelname == "WARNING":
            color = "yellow"
            self._record = None
            active_lines = 0
            truncate = Format.WRAP_OVERFLOW
        elif record.levelname == "PROMPT":
            color = "yellow"
            self._record = None
            active_lines = 0
            truncate = Format.WRAP_OVERFLOW
        elif record.levelname == "SUCCESS":
            self._record = None
            active_lines = 0
            truncate = Format.WRAP_OVERFLOW
        elif age:
            if age > 10:
                color = "yellow"
            if age > 30:
                color = "red"
            suffix = click.style(" [{:.1f}s]".format(age), fg=color)
        else:
            self._record = record
            self._last_update = time.time()

        prompt = click.style(f"{Character.LAMBDA}", fg=color)
        new_message = f"{self.clear_lines()}{prompt} {truncate}{message}{suffix}"

        timed_record = copy.copy(record)
        timed_record.msg = (
            f"{click.unstyle(new_message)}\n"
            # pyre-ignore[16]: Missing typeshed stub for this API
            if click.utils.should_strip_ansi(stream=sys.stderr)
            else new_message
        )
        self._active_lines = active_lines
        super(TimedStreamHandler, self).emit(timed_record)

    def _thread(self) -> None:
        while not self._terminate:
            record = self._record
            if record:
                age = time.time() - self._last_update
                if age > self.THRESHOLD:
                    self.emit(record, age)
            time.sleep(0.1)

    def terminate(self) -> None:
        self._terminate = True

        if self._active_lines > 0:
            click.echo(self.clear_lines(), file=sys.stderr, nl=False)
            self._active_lines = 0

        # Reset terminal.
        click.echo(Format.WRAP_OVERFLOW + Format.SHOW_CURSOR, file=sys.stderr, nl=False)
        sys.stderr.flush()


def initialize(noninteractive: bool) -> None:
    global __handler

    if __handler:
        LOG.debug("Log handler already exists, skipping initialization.")
        return
    if noninteractive:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(SectionFormatter())
        stream_handler.setLevel(logging.DEBUG)
        __handler = None
    else:
        stream_handler = TimedStreamHandler()
        __handler = stream_handler

    logging.addLevelName(PERFORMANCE, "PERFORMANCE")
    logging.addLevelName(PROMPT, "PROMPT")
    logging.addLevelName(SUCCESS, "SUCCESS")

    logging.basicConfig(level=logging.DEBUG, handlers=[stream_handler])


def start_logging_to_directory(noninteractive: bool, log_directory: str) -> None:
    if not noninteractive and log_directory is not None:
        if not os.path.exists(log_directory):
            os.makedirs(log_directory)
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_directory, "pyre.stderr"),
            mode="a",
            # Keep at most 5 log files on disk
            backupCount=4,
            # Limit the size of each log file to 10MB
            maxBytes=10 * 1000 * 1000,
        )
        handler.setFormatter(SectionFormatter())
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger()
        logger.addHandler(handler)


def cleanup() -> None:
    global __handler
    handler = __handler
    if handler:
        handler.terminate()
        __handler = None

    output = stdout.getvalue()
    if output:
        click.echo(output, nl=False)
        if not output.endswith("\n"):
            click.echo()


@contextlib.contextmanager
def configured_logger(noninteractive: bool) -> Generator[None, None, None]:
    try:
        initialize(noninteractive)
        yield
    finally:
        cleanup()


class StreamLogger:
    _should_stop_reading_stream = False
    _current_section: Optional[str]

    _server_log_pattern: Pattern[str] = re.compile(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\w+) (.*)"
    )

    def __init__(self, stream: Iterable[str]) -> None:
        self._reader = threading.Thread(target=self._read_stream, args=(stream,))
        self._reader.daemon = True
        self._current_section = None

    def join(self) -> None:
        self._reader.join()

    def _log_server_stderr_message(self, server_message: str) -> None:
        line = server_message.rstrip()
        match = self._server_log_pattern.match(line)
        if match:
            section = match.groups()[0]
            message = match.groups()[1]
            self._current_section = section
        else:
            section = self._current_section
            message = line

        if section == "ERROR":
            LOG.error(message)
        elif section == "INFO":
            LOG.info(message)
        elif section == "DUMP":
            LOG.warning(message)
        elif section == "WARNING":
            LOG.warning(message)
        elif section == "PROGRESS":
            LOG.info(message)
        elif section == "PARSER":
            LOG.error(message)
        elif section is not None:
            LOG.debug("[%s] %s", section, message)
        else:
            LOG.debug(line)

    def _read_stream(self, stream: Iterable[str]) -> None:
        try:
            for line in stream:
                if self._should_stop_reading_stream:
                    return
                self._log_server_stderr_message(line)
        except Exception:
            pass

    def __enter__(self) -> "StreamLogger":
        self._should_stop_reading_stream = False
        self._reader.start()
        return self

    def __exit__(
        self,
        _type: Optional[BaseException],
        _value: Optional[BaseException],
        _traceback: Optional[TracebackType],
    ) -> None:
        self._should_stop_reading_stream = True


def get_yes_no_input(prompt: str) -> bool:
    choice = get_input(prompt, suffix=" [Y/n] ")
    return choice.lower() in ["", "y", "ye", "yes"]


def get_optional_input(prompt: str, default: str) -> str:
    result = get_input(prompt, suffix=" (Default: `{}`): ".format(default))
    if result == "":
        return default
    return result


def get_input(prompt: str, suffix: str = "") -> str:
    LOG.log(PROMPT, prompt + suffix)
    return input().strip()
