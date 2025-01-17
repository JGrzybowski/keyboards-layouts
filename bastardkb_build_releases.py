#! /usr/bin/env python3

import argparse
import logging
import os
import os.path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from collections.abc import Callable, Sequence
from functools import partial, reduce
from logging.handlers import RotatingFileHandler
from operator import iconcat
from pathlib import Path, PurePath
from pygit2 import (
    GitError,
    Repository,
)
from rich.console import Console, Group
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from typing import NamedTuple, Optional


class Firmware(NamedTuple):
    keyboard: str
    keymap: str
    keymap_alias: Optional[str] = None
    env_vars: Sequence[str] = []

    @property
    def output_filename(self) -> str:
        return f"bastardkb_{self.keyboard.replace('/', '_')}_{self.keymap_alias or self.keymap}"

    def __str__(self) -> str:
        return f"{self.keyboard}:{self.keymap}"


class FirmwareList(NamedTuple):
    branch: str
    configurations: Sequence[Firmware]


DACMAN_KEYBOARD_FAMILY: Sequence[str] = (
    "skeletyl",
    "tbkmini",
    "scylla",
)

CHARYBDIS_KEYBOARD_FAMILY: Sequence[str] = (
    "charybdis/3x5",
    "charybdis/3x6",
    "charybdis/4x6",
)

ALL_BASTARD_KEYBOARDS: Sequence[str] = (
    *DACMAN_KEYBOARD_FAMILY,
    *CHARYBDIS_KEYBOARD_FAMILY,
)

AVR_MCUS: Sequence[str] = (
    "v1/elitec",
    "v2/elitec",
)

ARM_MCUS: Sequence[str] = (
    "blackpill",
    "v2/stemcell",
    "v2/splinky",
)

ALL_MCUS: Sequence[str] = (
    *AVR_MCUS,
    *ARM_MCUS,
)

ALL_FIRMWARES: Sequence[FirmwareList] = (
    # All firmwares built on the `bkb-master` branch, ie. the branch tracking
    # `qmk/qmk_firmware:master`.
    FirmwareList(
        branch="bkb-master",
        configurations=(
            # Use the `default` keymap for the non-Charybdis boards (ie. Scylla,
            # TBK mini, Skeletyl).  These board don't have a `via` keymap and
            # their "stock" configuration is using the `default` keymap instead.
            *tuple(
                Firmware(
                    keyboard=f"{keyboard}/{mcu}",
                    keymap="default",
                    keymap_alias="stock",
                )
                for keyboard in DACMAN_KEYBOARD_FAMILY
                for mcu in ALL_MCUS
            ),
            # Use the `via` keymap for the Charybdis boards (ie. the Charybdis,
            # Charybdis mini, and Charybdis nano).  These boards have a very
            # bare `default` keymap, and their "stock" configuration is using
            # the `via` keymap instead.
            *tuple(
                Firmware(
                    keyboard=f"{keyboard}/{mcu}",
                    keymap="via",
                    keymap_alias="stock",
                )
                for keyboard in CHARYBDIS_KEYBOARD_FAMILY
                for mcu in ALL_MCUS
            ),
            # Also build the Blackpill firmwares in uf2 format.
            *tuple(
                Firmware(
                    keyboard=f"{keyboard}/blackpill",
                    keymap="default",
                    keymap_alias="stock",
                    env_vars=("BOOTLOADER=tinyuf2",),
                )
                for keyboard in DACMAN_KEYBOARD_FAMILY
            ),
            *tuple(
                Firmware(
                    keyboard=f"{keyboard}/blackpill",
                    keymap="via",
                    keymap_alias="stock",
                    env_vars=("BOOTLOADER=tinyuf2",),
                )
                for keyboard in CHARYBDIS_KEYBOARD_FAMILY
            ),
            *tuple(
                Firmware(
                    keyboard=f"skeletyl/{mcu}",
                    keymap="manna-harbour_miryoku",
                    keymap_alias="miryoku",
                    env_vars=(
                        "MIRYOKU_ALPHAS=QWERTY",
                        "MIRYOKU_EXTRA=COLEMAKDH",
                    ),
                )
                for mcu in ALL_MCUS
            ),
            Firmware(
                keyboard="skeletyl/blackpill",
                keymap="manna-harbour_miryoku",
                keymap_alias="miryoku",
                env_vars=("BOOTLOADER=tinyuf2",),
            ),
            Firmware(keyboard="dilemma/assembled", keymap="via", keymap_alias="stock"),
            Firmware(keyboard="dilemma/splinky", keymap="via", keymap_alias="stock"),
        ),
    ),
    # All firmwares built on the `bkb-master-feat-zykrah-vial` branch, ie. the
    # branch tracking `zykrah/vial-qmk:vial-develop`.
    FirmwareList(
        branch="bkb-master-feat-zykrah-vial",
        configurations=(
            *tuple(
                Firmware(keyboard=f"{keyboard}/{mcu}", keymap="vial")
                for keyboard in ALL_BASTARD_KEYBOARDS
                for mcu in ARM_MCUS
            ),
            *tuple(
                Firmware(
                    keyboard=f"{keyboard}/blackpill",
                    keymap="vial",
                    env_vars=("BOOTLOADER=tinyuf2",),
                )
                for keyboard in ALL_BASTARD_KEYBOARDS
            ),
            Firmware(keyboard="dilemma/assembled", keymap="vial"),
            Firmware(keyboard="dilemma/splinky", keymap="vial"),
        ),
    ),
    # All LTS firmwares built on the `bkb-lts-vial` branch, ie. the branch
    # tracking a frozen version of `vial-kb/vial-qmk:vial`.
    # Vial-enabled Charybdis firmwares targeting AVR mcus are built against an
    # older version to ensure that the minimal viable feature set fits on the
    # controller.
    # See https://github.com/Bastardkb/bastardkb-qmk/issues/24 for more details.
    FirmwareList(
        branch="bkb-vial",
        configurations=(
            *tuple(
                Firmware(keyboard=f"{keyboard}/{mcu}", keymap="vial", keymap_alias="lts-vial")
                for keyboard in ALL_BASTARD_KEYBOARDS
                for mcu in AVR_MCUS
            ),
        ),
    ),
    # Firmware build off the `bkb-vial-feat-miryoku` branch that contains quick
    # and dirty changes to build a Vial-enabled Miryoku keymap for the Skeletyl.
    FirmwareList(
        branch="bkb-vial-feat-manna_harbour-miryoku",
        configurations=(
            *tuple(
                Firmware(
                    keyboard=f"skeletyl/{mcu}",
                    keymap="manna-harbour_miryoku",
                    keymap_alias="miryoku-vial",
                    env_vars=(
                        "KEY_OVERRIDE_ENABLE=no",
                        "LTO_ENABLE=yes",
                        "MIRYOKU_ALPHAS=QWERTY",
                        "MIRYOKU_EXTRA=COLEMAKDH",
                        "QMK_SETTINGS=no",
                        "SPACE_CADET_ENABLE=no",
                        "TAP_DANCE_ENABLE=no",
                        "VIALRGB_ENABLE=yes",
                        "VIAL_ENABLE=yes",
                        "VIAL_INSECURE=yes",
                        "VIA_ENABLE=yes",
                    ),
                )
                for mcu in AVR_MCUS
            ),
            *tuple(
                Firmware(
                    keyboard=f"skeletyl/{mcu}",
                    keymap="manna-harbour_miryoku",
                    keymap_alias="miryoku-vial",
                    env_vars=(
                        "LTO_ENABLE=no",
                        "MIRYOKU_ALPHAS=QWERTY",
                        "MIRYOKU_EXTRA=COLEMAKDH",
                        "VIA_ENABLE=yes",
                        "VIAL_ENABLE=yes",
                        "VIAL_INSECURE=yes",
                        "VIALRGB_ENABLE=yes",
                    ),
                )
                for mcu in ARM_MCUS
            ),
            Firmware(
                keyboard="skeletyl/blackpill",
                keymap="manna-harbour_miryoku",
                keymap_alias="miryoku-vial",
                env_vars=(
                    "BOOTLOADER=tinyuf2",
                    "LTO_ENABLE=no",
                    "MIRYOKU_ALPHAS=QWERTY",
                    "MIRYOKU_EXTRA=COLEMAKDH",
                    "VIA_ENABLE=yes",
                    "VIAL_ENABLE=yes",
                    "VIAL_INSECURE=yes",
                    "VIALRGB_ENABLE=yes",
                ),
            ),
        ),
    ),
)


class Reporter(object):
    def __init__(self, verbose: bool):
        self.console = Console()
        self.logging = logging.getLogger()
        self.verbose = verbose

        # Logging setup.
        logging_file_handler = RotatingFileHandler(
            filename=os.path.join(os.getcwd(), f"{os.path.basename(__file__)}.log"),
            encoding="utf-8",
            maxBytes=1024 * 1024,
            backupCount=5,
        )
        logging_file_handler.setFormatter(logging.Formatter(fmt="%(asctime)s %(levelname)s %(message)s"))
        self.logging.addHandler(logging_file_handler)
        self.logging.setLevel(level=logging.DEBUG)

        self.log_dir = tempfile.mkdtemp()
        self.debug(f"Saving logs in: {self.log_dir}")

        # Progress status.
        self._progress_status = lambda _: None

    def log_file(self, basename: str) -> Path:
        return Path(self.log_dir, basename).with_suffix(".log")

    def set_progress_status(self, progress_status: Callable[[str], None]) -> None:
        self._progress_status = progress_status

    def progress_status(self, message):
        self._progress_status(message)
        self.logging.info(message)

    def newline(self):
        self.console.print("")

    def debug(self, message) -> None:
        self.logging.debug(message)

    def info(self, message, **kwargs) -> None:
        self.console.print(message, **kwargs)
        self.logging.info(message)

    def warn(self, message, **kwargs) -> None:
        self.console.print(message, **kwargs)
        self.logging.warning(message)

    def error(self, message) -> None:
        self.console.print(message)
        self.logging.error(message)


class QmkCompletedProcess(object):
    def __init__(self, completed_process: subprocess.CompletedProcess, log_file: Path):
        self._completed_process = completed_process
        self.log_file = log_file

    @ property
    def returncode(self) -> int:
        return self._completed_process.returncode


class Executor(object):
    def __init__(self, reporter: Reporter, repository: Repository, dry_run: bool, parallel: int):
        self.dry_run = dry_run
        self.parallel = parallel
        self.reporter = reporter
        self.repository = repository

    def git_checkout(self, branch: str, update_submodules: bool) -> None:
        self.reporter.progress_status(f"Checking out [bright_magenta]{branch}[/bright_magenta]…")
        try:
            branch_ref = self.repository.branches[branch]
        except KeyError:
            self.reporter.error("Branch does not exist")
            sys.exit(1)
        if not self.dry_run:
            self.repository.checkout(branch_ref)
            if update_submodules:
                self.reporter.progress_status(f"([bright_magenta]{branch}[/bright_magenta]) Updating submodules…")
                # TODO(0xcharly): use pygit2 to update submodules.
                self._run(
                    ("git", "submodule", "update", "--init", "--recursive"),
                    log_file=self.reporter.log_file(f"git-submodule-update-{branch}"),
                    cwd=self.repository.workdir,
                )
        else:
            self.reporter.progress_status(f"([bright_magenta]{branch}[/bright_magenta]) Updating submodules…")

    def qmk_compile(self, firmware: Firmware) -> QmkCompletedProcess:
        self.reporter.progress_status(f"Compiling [bold white]{firmware}[/bold white]")
        argv = (
            "qmk",
            "compile",
            "--clean",
            "--parallel",
            str(self.parallel),
            "--keyboard",
            f"bastardkb/{firmware.keyboard}",
            "--keymap",
            firmware.keymap,
            "--env",
            f"TARGET={firmware.output_filename}",
            # "--env",
            # f"USE_CCACHE=yes",
            *reduce(iconcat, (("-e", env_var) for env_var in firmware.env_vars), []),
        )
        log_file = self.reporter.log_file(f"qmk-compile-{firmware.output_filename}")
        return QmkCompletedProcess(self._run(argv, log_file=log_file), log_file)

    def _run(
        self,
        argv: Sequence[str],
        log_file: Path,
        **kwargs,
    ) -> subprocess.CompletedProcess:
        self.reporter.debug(f"exec: {shlex.join(argv)}")
        self.reporter.debug(f"output: {log_file}")
        if not self.dry_run:
            with log_file.open("w") as fd:
                kwargs["stdout"] = fd
                kwargs["stderr"] = fd
                return subprocess.run(argv, **kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0)


def total_firmware_count_reduce_callback(acc: int, firmware_list: FirmwareList) -> int:
    return acc + len(list(firmware_list.configurations))


def read_firmware_filename_from_logs(firmware: Firmware, log_file: Path) -> Path:
    pattern = re.compile(
        f"Copying (?P<filename>{re.escape(firmware.output_filename)}\\.[a-z0-9]+) to qmk_firmware folder"
    )
    with log_file.open() as fd:
        for line in fd:
            match = pattern.match(line)
            if match:
                return Path(match.group("filename"))
    raise FileNotFoundError()


def build(
    executor: Executor,
    reporter: Reporter,
    firmwares: Sequence[FirmwareList],
    on_firmware_compiled: Callable[[Path], None],
) -> None:
    empty_status = Progress(TextColumn(""))
    overall_status = Progress(SpinnerColumn(), TextColumn("{task.description}"))
    overall_progress = Progress(
        MofNCompleteColumn(),
        BarColumn(complete_style="blue"),
        TextColumn("[magenta]{task.percentage:>5.1f}%"),
        TimeElapsedColumn(),
        console=reporter.console,
    )
    progress_group = Group(empty_status, overall_status, overall_progress)

    total_firmware_count = reduce(total_firmware_count_reduce_callback, firmwares, 0)
    built_firmware_count = 0
    newline_task = empty_status.add_task("")
    overall_status_task = overall_status.add_task("Preparing…")
    overall_progress_task = overall_progress.add_task("", total=total_firmware_count)
    reporter.set_progress_status(lambda message: overall_status.update(overall_status_task, description=message))
    reporter.info(f"Preparing to build {total_firmware_count} BastardKB firmwares")
    with Live(progress_group, console=reporter.console):
        for branch, configurations in firmwares:
            # Checkout branch.
            reporter.info(f"  Building off branch [magenta]{branch}[/] ({len(configurations)} firmwares)")
            executor.git_checkout(branch, update_submodules=True)

            # Build firmwares off that branch.
            for firmware in configurations:
                completed_process = executor.qmk_compile(firmware)
                if completed_process.returncode == 0:
                    try:
                        on_firmware_compiled(read_firmware_filename_from_logs(firmware, completed_process.log_file))
                        built_firmware_count += 1
                        reporter.info(
                            f"    CC [not bold white]{str(firmware):46}[/] [green]SUCCESS[/]"
                        )
                    except FileNotFoundError:
                        reporter.warn(
                            f"    CC [not bold white]{str(firmware):46}[/] [yellow]WARNING[/]"
                        )
                else:
                    reporter.error(f"    CC [not bold white]{str(firmware):46}[/] [red]FAILURE[/]")
                    reporter.error(f"Logs: {completed_process.log_file}")
                overall_progress.update(overall_progress_task, advance=1)
            reporter.newline()
        overall_status.update(overall_status_task, visible=False)
        empty_status.update(newline_task, visible=False)
        reporter.info(f"Done: built={built_firmware_count}, failed={total_firmware_count - built_firmware_count}")


def copy_firmware_to_output_dir(reporter: Reporter, output_dir: Path, repository_path: Path, firmware_filename: Path):
    try:
        firmware_file = repository_path / firmware_filename
        target = output_dir / firmware_file.name
        if firmware_file != target:
            reporter.logging.debug(f"copy: {firmware_file} -> {target}")
            firmware_file.rename(target)
        else:
            reporter.logging.debug(f"firmware already at {firmware_file}")
    except OSError:
        reporter.logging.exception("failed to copy firmware to output directory")


def copy_assets_to_output_dir(executor: Executor, reporter: Reporter, output_dir: Path, repository_path: Path):
    reporter.newline()
    reporter.info("Copying BastardKB firmwares assets")
    executor.git_checkout('main', update_submodules=False)

    try:
        via_json_dir = (repository_path / 'via').resolve()
    except FileExistsError:
        reporter.error("Cannot find Via's JSON files directory")
        return

    if not via_json_dir.is_dir():
        reporter.error(f"{via_json_dir} is not a directory")
        return

    via_json_list = [f for f in via_json_dir.glob('*.via.json') if f.is_file()]
    reporter.info(f"  Copying [magenta]Via[/] definition files ({len(via_json_list)} files)")
    for src in via_json_list:
        dst = output_dir / src.name
        if not executor.dry_run:
            shutil.copyfile(src, dst)
        reporter.info(f"    CP [not bold white]{str(src.name):46}[/] [green]DONE[/]")
        reporter.logging.debug(f"copy: {src} -> {dst}")


def sigint_handler(reporter: Reporter, signal, frame):
    del signal, frame
    reporter.progress_status("Interrupted.  Exiting…")
    sys.exit(1)


def main() -> None:
    # Parse command line arguments.
    parser = argparse.ArgumentParser(description="Create Bastard Keyboard firmware release.")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Don't actually build, just show the commands to be run.",
    )
    parser.add_argument(
        "-j",
        "--parallel",
        type=int,
        help="Parallel option to pass to qmk-compile",
        default=1,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output.")
    parser.add_argument(
        "-r",
        "--repository",
        type=PurePath,
        help="The QMK repository checkout to work with",
        default=Path.cwd(),
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="The output directory in which to copy the artifacts",
        default=Path.cwd(),
    )
    cmdline_args = parser.parse_args()
    reporter = Reporter(cmdline_args.verbose)

    # Install SIGINT handler.
    signal.signal(signal.SIGINT, partial(sigint_handler, reporter))

    # Open QMK repository.
    try:
        repository = Repository(cmdline_args.repository)
    except GitError:
        reporter.error("Failed to initialize QMK repository")
        sys.exit(1)

    # Create output dir if needed.
    try:
        cmdline_args.output_dir.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        reporter.error("Output path exists and is not a directory")
        sys.exit(1)

    # Create the process dispatcher.
    executor = Executor(reporter, repository, cmdline_args.dry_run, cmdline_args.parallel)

    # Build the firmwares and copy them to the ouptut directory.
    build(
        executor,
        reporter,
        ALL_FIRMWARES,
        partial(
            copy_firmware_to_output_dir,
            reporter,
            cmdline_args.output_dir,
            cmdline_args.repository,
        ),
    )

    # Copy assets.
    copy_assets_to_output_dir(executor, reporter, cmdline_args.output_dir, cmdline_args.repository)


if __name__ == "__main__":
    main()
