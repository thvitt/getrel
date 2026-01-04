# `getrel` - A command-line tool for managing software releases from GitHub.

This document provides a comprehensive overview of the `getrel` project, its structure, and how to use it.

## Project Overview

`getrel` is a Python-based command-line tool designed to simplify the process of tracking and managing software releases from GitHub. It allows users to define a list of projects they are interested in, and then use `getrel` to:

*   Check for new releases.
*   Download release artifacts.
*   Perform actions on the downloaded files, such as unpacking archives and creating symbolic links.
*   List the managed projects and their current version status.

The tool is built with a modular architecture, with clear separation between the command-line interface, configuration management, and the logic for interacting with GitHub.

### Key Technologies

*   **Python:** The core language of the project.
*   **cyclopts:** For building the command-line interface.
*   **httpx:** For making HTTP requests to the GitHub API.
*   **msgspec:** For efficient serialization and deserialization of configuration and state files.
*   **rich:** For providing rich, formatted output in the terminal.
*   **pyyaml:** For parsing YAML configuration files.

## Building and Running

The project uses `uv` for dependency management and building.

### Setup

1.  **Install dependencies:**
    ```bash
    uv sync
    ```

### Running Tests

The project uses `pytest` for testing.

```bash
pytest
```

### Running the tool

The main entry point for the tool is the `getrel` command.

```bash
getrel --help
```

## Development Conventions

*   **Configuration:** Project configurations are stored in YAML files located in `~/.config/getrel/projects/`. Each project has its own YAML file (e.g., `my-project.yaml`).
*   **State:** The application state (e.g., installed versions) is stored in a msgpack file at `~/.local/share/getrel/projects.msgpack`.
*   **Code Style:** The project uses `ruff` for linting and formatting. The configuration for `ruff` can be found in the `pyproject.toml` file.

## Configuration

To add a new project for `getrel` to manage, you need to create a YAML file in the configuration directory (`~/.config/getrel/projects/`).

### Example Configuration

Here's an example of a project configuration file (e.g., `~/.config/getrel/projects/my-cool-tool.yaml`):

```yaml
name: my-cool-tool
kind: github
user: some-user
repo: some-repo
download:
  - "my-cool-tool-*.tar.gz"
install:
  - action: unpack
    source: "my-cool-tool-*.tar.gz"
  - action: bin
    source: "my-cool-tool-*/my-cool-tool"
    link: "~/.local/bin/"
```

This configuration defines a project named `my-cool-tool` that is hosted on GitHub. `getrel` will look for releases in the `some-user/some-repo` repository. When a new release is found, it will download the tarball matching the `download` pattern, unpack it, and then create a symbolic link to the binary in `~/.local/bin/`.
