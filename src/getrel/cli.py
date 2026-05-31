import dataclasses
import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterable
from fnmatch import fnmatch
from pathlib import Path
from textwrap import indent
from typing import Annotated

import httpx
import xdg.BaseDirectory
from cyclopts import App, Parameter, validators
from cyclopts.group import Group
from msgspec import yaml
from rich import get_console
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.progress import DownloadColumn, Progress, track
from rich.table import Column, Table
from rich.text import Text
from rich.traceback import install as install_rich_traceback

from getrel.actions import BinAction, Project, ProjectState, Settings, write_schemas
from getrel.add import PreferenceScores, identifying_pattern
from getrel.add import add as add_
from getrel.config import (
    first_config_path,
    load_project_config,
    load_project_configs,
    load_project_states,
    save_state,
)
from getrel.convert import convert_file, convert_state
from getrel.github import GithubProjectManager
from getrel.utils import WorkingDirectory, enc_hook, unique

logger = logging.getLogger(__name__)

app = App(verbose=False)
app.register_install_completion_command(add_to_startup=False)

# Command groups
plumbing = Group("Plumbing")
management = Group("Management")
infos = Group("Info")

selection = Group(validator=validators.mutually_exclusive)


@app.meta.default
def prepare(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    verbose: Annotated[int, Parameter(alias="-v", count=True)] = 0,
):
    """
    Args:
        verbose: Report what is done. Repeatable for increasing amount of debugging info.
    """
    global_level = logging.WARNING - (verbose // 2) * 10
    local_level = logging.WARNING - ((verbose + 1) // 2 * 10)
    logging.basicConfig(
        format="%(message)s (%(name)s)",
        handlers=[
            RichHandler(
                console=app.error_console, rich_tracebacks=local_level <= logging.DEBUG
            )
        ],
    )
    if local_level <= logging.DEBUG:
        install_rich_traceback(console=app.error_console, suppress=["cyclopts"])
    logging.getLogger().setLevel(global_level)
    logging.getLogger("getrel").setLevel(local_level)

    app(tokens)


@app.command(group=plumbing)
def convert_old_config(
    input: Path | None = first_config_path("getrel", "projects.toml"),  # noqa: A002, B008
    /,
    output: Annotated[Path, Parameter(alias="-o")] = Path(
        xdg.BaseDirectory.xdg_config_home, "getrel", "projects"
    ),
):
    """
    Convert the old getrel configuration to the new format.
    """
    if input is None:
        logger.critical("No old getrel config found. Exiting.")
        return 2
    convert_file(input, output)


@app.command(group=plumbing)
def convert_old_state():
    """
    Convert the old getrel state to the new format.
    """
    states: dict[str, ProjectState] = {}
    for root in xdg.BaseDirectory.load_data_paths("getrel"):
        for project in Path(root).iterdir():
            if not project.is_dir():
                continue
            name = project.name
            try:
                state = convert_state(project / ".getrel")
                states[name] = state
            except Exception as e:
                logger.error("Failed to read state for %s: %s", name, e)
    save_state(states)


def _format_binary(binary: Path):
    cmd = binary.name
    found = shutil.which(binary.name)
    if not found:
        return Text(str(binary), style="warning")
    elif binary.samefile(found):
        return Text(cmd, style="bold")
    else:
        return Text(cmd, style="red")


@app.command(name="list", group=infos)
def list_projects(
    projects: Annotated[list[str] | None, Parameter(group=selection)] = None,
    new: Annotated[
        bool, Parameter(alias="-n", negative=False, group=selection)
    ] = False,
):
    """
    List the configured projects.

    Args:
        projects: only list specific projects, identified by name
        new: only list projects for which updates are available
    """
    configs = {project.name: project for project in load_project_configs()}
    states = load_project_states()
    if projects is None:
        projects = list(configs)  # list({*configs, *states})
    table = Table(
        Column("Name", style="bold"),
        "Version ([green]update[/green], [red]not installed[/red])",
        "Binaries ([red]shadowed[/red], [dim]missing[/dim])",
        "description",
        box=None,
    )
    for project in projects:
        version = "?"
        description = "[dim]no info yet[/dim]"
        binaries = ""
        installed = False
        updatable = False
        if project in states:
            state = states[project]
            description = state.description
            if state.installed:
                installed = True
                if state.updateable:
                    version = f"{state.installed.version} → [bold green]{state.available.version}[/bold green]"  # pyright: ignore[reportOptionalMemberAccess]
                    updatable = True
                else:
                    version = f"{state.installed.version}"
                binaries = Text(" ").join(
                    _format_binary(binary)
                    for binary in state.get_installed(
                        binary=True, external=True, absolute=True
                    )
                )
            elif state.available:
                version = f"[red]{state.available.version}"
                binaries = Text(
                    " ".join(
                        action.bin or action.source
                        for action in configs[project].install
                        if isinstance(action, BinAction)
                    ),
                    style="dim",
                )
        if not new or updatable:
            table.add_row(
                project,
                version,
                binaries,
                description,
                style="dim" if not installed else None,
            )
    get_console().print(table)


def _all_files(top: Path):
    for root, dirs, files in top.walk():
        for name in dirs:
            yield root / name
        for name in files:
            yield root / name


def _ls_files(files: Iterable[Path], sep="\n  ") -> str:
    return sep + sep.join(map(str, files))


@app.command
def check(projects: list[str] | None = None):
    """
    Check consistency between configuration, state and actual data on the hard disk.

    Args:
        projects: Only check specific projects.
    """
    configs = {project.name: project for project in load_project_configs()}
    states = load_project_states()
    projects = projects or list({*configs, *states})

    for project in projects:
        if project not in configs:
            logger.error("Project %s does not have a config", project)
        if project not in states:
            logger.info(
                "No status known for project %s. Run %s update", project, app.name
            )
        else:
            state = states[project]
            with WorkingDirectory(state.project_dir):
                if not state.installed and state.installed_files:
                    logger.error(
                        "Project %s is not installed, but has %d installed files: %s",
                        project,
                        len(state.installed_files or []),
                        _ls_files(state.installed_files),
                    )
                if state.installed and not state.installed_files:
                    logger.warning(
                        "Project %s is installed, but has no installed files", project
                    )
                missing_files = [
                    file for file in state.installed_files if not Path(file).exists()
                ]
                if missing_files:
                    logger.error(
                        "Project %s: %d files are marked as installed, but cannot be found: %s",
                        project,
                        len(missing_files),
                        _ls_files(missing_files),
                    )
                extra_files = set(_all_files(Path())) - set(state.installed_files)
                if extra_files:
                    logger.warning(
                        "Project %s has %d extra files in its project directory: %s",
                        project,
                        len(extra_files),
                        _ls_files(extra_files),
                    )


def _resolve_installed_file(file: Path, project_dir: Path) -> Path:
    return file if file.is_absolute() else project_dir / file


def _is_asset(filename: str, config: Project) -> bool:
    settings = Settings.load()
    return any(
        fnmatch(filename, exp_pat)
        for pat in config.download
        for exp_pat in settings.expand_arch(pat)
    )


def _classify_install_status(
    state: ProjectState, existing: list[Path], missing: list[Path]
) -> str:
    total = len(existing) + len(missing)
    if not state.installed:
        return "[dim]not installed[/dim]"
    if not state.installed_files:
        return "[yellow]installed (no files tracked)[/yellow]"
    if not missing:
        return f"[green]installed ({total} files)[/green]"
    if not existing:
        return f"[red]uninstalled ({total} tracked files missing)[/red]"
    return (
        f"[yellow]partially installed ({len(existing)}/{total} files present)[/yellow]"
    )


def _scan_project_dir(
    project_dir: Path, known_abs: set[Path], config: Project | None
) -> tuple[list[Path], list[Path]]:
    """Returns (old_versions, unknown_files) for on-disk files not in the installed state."""
    old_versions: list[Path] = []
    unknown_files: list[Path] = []
    for root, dirs, files in project_dir.walk():
        if root == project_dir:
            dirs[:] = [d for d in dirs if d != ".getrel"]
        for fname in files:
            fpath = root / fname
            if fpath.resolve() in known_abs:
                continue
            if config is not None and _is_asset(fname, config):
                old_versions.append(fpath)
            else:
                unknown_files.append(fpath)
    return old_versions, unknown_files


@dataclasses.dataclass
class _RepairCtx:
    dry_run: bool
    reinstall: bool
    remove_unknown: bool
    remove_old_versions: bool
    console: Console


@dataclasses.dataclass(frozen=True)
class _FileKind:
    label: str
    style: str
    flag_name: str
    remove: bool


def _handle_extra_files(
    files: list[Path], project_dir: Path, kind: _FileKind, ctx: _RepairCtx
) -> None:
    if not files:
        return
    for f in files:
        rel = f.relative_to(project_dir)
        if kind.remove:
            verb = "would remove" if ctx.dry_run else "removing"
            ctx.console.print(
                f"  [{kind.style}]{verb} {kind.label}:[/{kind.style}] {rel}"
            )
            if not ctx.dry_run:
                f.unlink()
        else:
            ctx.console.print(f"  [{kind.style}]{kind.label}:[/{kind.style}] {rel}")
    if not kind.remove:
        noun = f"{len(files)} {kind.label}{'s' if len(files) != 1 else ''}"
        ctx.console.print(f"  [dim]({noun}; use {kind.flag_name} to delete)[/dim]")


def _handle_reinstall(
    state: ProjectState, config: Project, project_dir: Path, ctx: _RepairCtx
) -> bool:
    """Offers or performs reinstall. Returns True if state was changed."""
    assets = [
        f for f in project_dir.iterdir() if f.is_file() and _is_asset(f.name, config)
    ]
    if not assets:
        return False
    if ctx.reinstall:
        verb = "Would reinstall" if ctx.dry_run else "Reinstalling"
        ctx.console.print(f"  [green]{verb} using {len(assets)} asset(s)[/green]")
        if not ctx.dry_run:
            state.installed_files = [a.relative_to(project_dir) for a in assets]
            config.do_install(state)
            state.installed_files = list(unique(state.installed_files))
            return True
    else:
        ctx.console.print(
            f"  [dim]{len(assets)} asset(s) present; use --reinstall to reinstall[/dim]"
        )
    return False


def _repair_project(
    project_name: str, state: ProjectState, config: Project | None, ctx: _RepairCtx
) -> bool:
    """Repairs a single project. Returns True if the state was changed."""
    project_dir = state.project_dir

    original_count = len(state.installed_files)
    state.sanitize_files()
    dup_count = original_count - len(state.installed_files)

    existing: list[Path] = []
    missing: list[Path] = []
    known_abs: set[Path] = set()
    for file in state.installed_files:
        resolved = _resolve_installed_file(file, project_dir)
        known_abs.add(resolved.resolve())
        if resolved.exists():
            existing.append(file)
        else:
            missing.append(file)

    old_versions: list[Path] = []
    unknown_files: list[Path] = []
    if project_dir.exists():
        old_versions, unknown_files = _scan_project_dir(project_dir, known_abs, config)

    if not (dup_count or missing or old_versions or unknown_files):
        return False

    ctx.console.print(
        f"[bold]{project_name}[/bold]: {_classify_install_status(state, existing, missing)}",
        highlight=False,
    )
    if dup_count:
        verb = "would remove" if ctx.dry_run else "removed"
        ctx.console.print(
            f"  [dim]{verb} {dup_count} duplicate state entr{'y' if dup_count == 1 else 'ies'}[/dim]"
        )
    for f in missing:
        ctx.console.print(f"  [red]✗[/red] {f}")

    was_installed = bool(state.installed)
    state_changed = False
    if missing:
        state.installed_files = existing
        if not existing and state.installed:
            state.installed = None
        state_changed = not ctx.dry_run
    elif dup_count:
        state_changed = not ctx.dry_run

    if (
        missing
        and was_installed
        and config
        and project_dir.exists()
        and _handle_reinstall(state, config, project_dir, ctx)
    ):
        state_changed = True

    _handle_extra_files(
        old_versions,
        project_dir,
        _FileKind(
            "old version", "yellow", "--remove-old-versions", ctx.remove_old_versions
        ),
        ctx,
    )
    _handle_extra_files(
        unknown_files,
        project_dir,
        _FileKind("unknown", "red", "--remove-unknown", ctx.remove_unknown),
        ctx,
    )

    return state_changed


@app.command(group=management)
def repair(
    projects: list[str] | None = None,
    /,
    *,
    remove_unknown: Annotated[bool, Parameter(alias="-u", negative=())] = False,
    remove_old_versions: Annotated[bool, Parameter(alias="-o", negative=())] = False,
    reinstall: Annotated[bool, Parameter(alias="-r", negative=())] = False,
    dry_run: Annotated[bool, Parameter(alias="-n", negative=())] = False,
):
    """
    Check and repair the state of installed projects.

    For each project, validates that all listed installed files exist, removes
    missing entries from the state, finds unknown or outdated files in the project
    directory, and deduplicates the installed file list.

    Args:
        projects: Only repair specific projects.
        remove_unknown: Remove files in the project directory that are not listed as installed and do not match the download pattern.
        remove_old_versions: Remove files matching the download pattern that are not listed as installed (old downloaded versions).
        reinstall: Reinstall partially installed projects whose downloaded artifacts are still present.
        dry_run: Report what would be changed without making any changes.
    """
    configs = {p.name: p for p in load_project_configs()}
    states = load_project_states()
    projects = projects or list(states)
    ctx = _RepairCtx(
        dry_run=dry_run,
        reinstall=reinstall,
        remove_unknown=remove_unknown,
        remove_old_versions=remove_old_versions,
        console=get_console(),
    )
    state_changed = False

    for project_name in projects:
        if project_name not in states:
            logger.info("No state for project %s", project_name)
            continue
        if _repair_project(
            project_name, states[project_name], configs.get(project_name), ctx
        ):
            state_changed = True

    if state_changed and not dry_run:
        save_state(states)


@app.command(group=infos)
def info(project: str, /):
    """Print information about a specific project."""
    state = load_project_states()[project]
    config = load_project_config(project)
    md = Table(show_header=False, show_edge=False, highlight=True)

    md.add_row(project, state.description, style="bold")
    md.add_row("URL", config.url)
    md.add_row("Download", ", ".join(config.download))
    _install = "\n".join(f"* {action}" for action in config.install)
    if _install:
        md.add_row("Install", Markdown(_install))
    _uninstall = "\n".join(f" {action}" for action in config.uninstall)
    if _uninstall:
        md.add_row("Uninstall", Markdown(_uninstall))
    if state.available:
        md.add_row("Release Notes", Markdown(state.available.description or ""))
    get_console().print(md)


def capture(*objects, **kwargs) -> str:
    console = Console(force_terminal=False, force_interactive=False, record=True)
    console.begin_capture()
    console.print(*objects, **kwargs)
    return console.end_capture()


@app.command(group=infos)
def ls(project: str | None = None):
    """Summarize the installed files of the given project."""
    if project is None:
        return list_projects()
    states = load_project_states()
    if project in states:
        config = load_project_config(project)
        summary = states[project].summarize_directory(config)
        get_console().print(summary)
    else:
        logger.error("Project %s is not installed", project)


@app.command(group=management)
def edit(project: str):
    """
    Edit the configuration of the given project.
    """
    if project == "settings":
        config_path = (
            Path(xdg.BaseDirectory.save_config_path("getrel")) / "settings.yaml"
        )
        schema_filename = "getrel-settings.schema.json"
        config = None
    else:
        config = load_project_config(project)
        config_path = config.project_file
        schema_filename = "getrel-project.schema.json"

    if not (config_path.parent / schema_filename).exists():
        save_schemas()

    content = config_path.read_text() if config_path.exists() else ""
    schema_comment = f"# yaml-language-server: $schema={schema_filename}"
    if not content.startswith(schema_comment):
        if content.startswith("# yaml-language-server:"):
            lines = content.splitlines()
            lines[0] = schema_comment
            content = "\n".join(lines) + ("\n" if lines else "")
        else:
            content = schema_comment + "\n" + content
        config_path.write_text(content)

    states = load_project_states()
    if config and project in states:
        summary_tree = states[project].summarize_directory(config)
        summary_text = capture(summary_tree)
        commented_summary = indent(summary_text, "# ").rstrip()

        content = config_path.read_text()
        lines = content.splitlines()

        summary_header = f"# {project} ("
        start_index = -1
        for i, line in enumerate(lines):
            if line.startswith(summary_header) and line.endswith(")"):
                start_index = i
                break

        new_lines = lines[:start_index] if start_index != -1 else lines

        while new_lines and not new_lines[-1].strip():
            new_lines.pop()

        new_content = "\n".join(new_lines) + "\n\n" + commented_summary + "\n"
        config_path.write_text(new_content)

    editor = os.environ.get("EDITOR", "vi") or "vi"
    subprocess.call([*shlex.split(editor), str(config_path)])


@app.command(group=management)
def update(prerelease: bool = False):
    """Update all projects metadata and list the projects with updates."""
    with Progress(transient=True) as progress:
        manager = GithubProjectManager()
        new = manager.look_for_new_versions(progress=progress, prerelease=prerelease)
        list_projects([p.name for p in new])


@app.command(group=management)
def upgrade(
    projects: Annotated[list[str] | None, Parameter(group=selection)] = None,
    /,
    *,
    update: Annotated[
        bool, Parameter(alias="-u", negative=(), group=selection)
    ] = False,
    prerelease: bool = False,
):
    """
    Upgrade the given or all updatable projects.

    Args:
        projects: If given, only update the listed projects.
        update: Run update first, i.e., check which projects are updateable.
        prerelease: Include prereleases.
    """
    manager = GithubProjectManager()
    with (
        httpx.Client() as client,
        Progress(
            *Progress.get_default_columns(),
            DownloadColumn(),
            transient=True,
            console=app.error_console,
        ) as progress,
    ):
        if update:
            manager.look_for_new_versions(progress=progress, prerelease=prerelease)
        if projects is None:
            projects = manager.updateable_projects()
        for project, artefacts in progress.track(
            manager.list_artifacts(projects, prerelease=prerelease),
            description="Getting artefacts ...",
            total=len(projects),
        ):
            try:
                assets = list(manager.download(project, artefacts, client, progress))
                manager.install_or_update(project, assets)
                manager.states[project.name].installed = manager.states[
                    project.name
                ].available
                save_state(manager.states)
            except Exception as e:
                logger.error(
                    "Failed to install %s: %s",
                    project.name,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )


@app.command(group=plumbing)
def dump_assets(projects: list[str] | None = None):
    """
    Dump the information on all artefacts of the given projects to stdout.
    """
    manager = GithubProjectManager()
    scorer = PreferenceScores.load()
    result = {}
    if projects is None:
        projects = list(manager.configs)
    for project, artefacts in track(manager.list_artifacts(projects), transient=True):
        scores = [
            {
                "artefact": artefact,
                "score": scorer.rate(scorer.assets, artefact, Settings.load()),
            }
            for artefact in artefacts
        ]
        scored_artfs = sorted(
            scores,
            key=lambda d: (d["score"], d["artefact"].downloadCount),
            reverse=True,
        )
        pat = identifying_pattern(
            [a.name for a in artefacts],
            scored_artfs[0]["artefact"].name,
            version=manager.states[project.name].available.version,
        )
        result[project.name] = {
            "configured": project.download,
            "generated": pat,
            "artefacts": scored_artfs,
        }
    print(yaml.encode(result, enc_hook=enc_hook).decode())


@app.command(group=management)
def install(
    projects: list[str] | None = None,
    /,
    *,
    missing: Annotated[bool, Parameter(alias="-m", negative=())] = False,
):
    """
    Install the given (or all missing) projects.

    Args:
        projects: Names of the projects to install.
        missing: Install all projects that are configured, but not installed.

    Returns:
        1 if nothing to install
    """
    projects = projects or []
    manager = GithubProjectManager()
    if missing:
        projects.extend(
            project for project in manager.configs if not manager.installed(project)
        )
    if not projects:
        logger.error("No projects to install.")
        return 1
    return upgrade(projects)


@app.command(group=management)
def uninstall(
    projects: list[str],
    /,
    *,
    delete_assets: Annotated[bool, Parameter(alias="-a")] = False,
    delete_config: Annotated[bool, Parameter(alias="-c")] = False,
):
    """
    Uninstall the given projects.

    Args:
        projects: Names of the project(s) to remove.
        delete_assets: Also remove the downloaded files.
    """
    manager = GithubProjectManager()
    for project in projects:
        manager.uninstall(project, delete_assets=delete_assets)
        if delete_config:
            manager.delete_config(project)


@app.command(group=management)
def clean_state(dry_run: bool = False):
    """Remove the state for all projects that do not have a configuration."""
    configs = {config.name: config for config in load_project_configs()}
    state = load_project_states()
    unconfigured = set(state) - set(configs)
    if unconfigured:
        logger.warning(
            "The following projects no longer have a configuration file, their state will be removed %s",
            ", ".join(unconfigured),
        )
        for name in unconfigured:
            del state[name]
        if not dry_run:
            save_state(state)
    else:
        logger.info("All projects that have a state also have a configuration.")


@app.command(group=management, alias="rm")
def remove(projects: list[str], /):
    """Remove the projects, all its files and the project's configuration."""
    uninstall(projects, delete_assets=True, delete_config=True)


@app.command(group=plumbing)
def save_schemas():
    """Save the JSON schemas for the configuration files to the settings directory."""
    write_schemas()


@app.command(group=management)
def add(url: str, prerelease: Annotated[bool, Parameter(alias="-p")] = False):
    """
    Add a new project. Work in progress, you might want to use edit afterwards.

    Args:
        url: URL to the project
        prerelease: if true, consider prereleases
    """
    add_(url, prerelease=prerelease)
