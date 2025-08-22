# Concepts

## Project

A project describes a single software package that can be installed. A project
is defined by a **project configuration** that is stored below
`~/.config/getrel` and that describes how to find new versions and what to do to
install the project. It has a **state** that describes all the dynamic
information getrel needs to know about the project – whether it is installed,
which release is installed, when we have last checked for new versions, which
files were installed. For each installed or downloaded project, there is a
**project directory** in which the project’s files can be stored.

A project is associated with a **project type** that implements the actual
checking and downloading of the project — at first, we only implement _GitHub
Releases_ as a project kind. Operations (like update or download) are run
grouped by kind through a **project type manager** that may use a less costly
combined way than to check each individual file.

## Operations

Each project offers the following **basic operations**. Projects or project type
managers can cache and group those actions. Operations marked with a * need to
be implemented per project type, for the others there are default
implementations.

### Check versions \*

Check which versions of a software exists, or whether a new relevant version
(according to the project configuration) exists.

### List Version Assets \*

Lists all files that can be downloaded for a specific version.

### Download Version \*

Downloads all configured files for a specific version.

### Install Version

Run a list of configured **actions** to install the software.

### Uninstall Version

Remove all files installed for a version.

### Delete Project

Uninstall the installed version(s), if any, and delete the state and
configuration files for the project.

### Backup

Creates a copy of the files of an installed tool.

### Restore

Restores the last backup.

## Installation Actions

Installing a version is completely configurable via defining a sequence of
actions in the project configuration. Common to all actions:

- `action` defines the kind of action, the kinds will be the following headings.
- `source` is a glob pattern that in a way defines on which file(s) the action
  will work. Details are specific to the action, but by default this will be
  expanded in the project directory.
- Each action is run in the project directory, after all assets have been
  downloaded.
- Each action will be passed a list of files that are already installed for the
  project. It should add all files it creates and remove all files it deletes.

### unpack

Extract all files from an archive. Works with ZIP and TAR archives and various
compression methods. Optional additional configuration:

- `destination` (path, default: the project directory) unpack to this directory.
- `delete-archive` (boolean, default: false): remove the archive after
  unpacking.

### link

For each source, create a symbolic link that links to the file in the project
directory. Options:

- `link` (path, required): Where the link will live. When ending with `/`,
  equivalent to `dir=true`
- `dir` (bool, optional): if true, `link` will be assumed to be a directory
  which will be created when it does not exist jet.
- absolute (bool default false): Whether the link should be forced to be
  absolute.

If the path pointed to by `link` is an existing directory, if it ends with / or
if `dir` is true, respective source's file name will be used as a filename in
that directory to define the link. Otherwise, the link specifies the full name
to be created. Non-existing parents will be created.

### bin

A specialisation of `link` specifically crafted to install executables.

- `link` defaults to `~/.local/bin/`
- `bin` (optional, str or path) the binary name, see below for details.

For each source, we make sure that its executable bit is set. For each source,
the link name will be determined as follows:

- if `bin` is not present the rules are as for link (but remember the default
  value for link!)
- if bin is present and relative, resolve it relative to link
- if bin is present and absolute, assume it as full link name and ignore link

### Script actions: install-script, uninstall-script, post-uninstall-script

Runs a command or an (inline) script.

There are three alternative ways how the command is run:

- Specify an executable and its arguments using the `cmd` option.

  If present, the option’s string value is split into words according to
  shell splitting rules. Each word is variable and user expanded, and the
  first word is searched for in the $PATH.

- Specify a shell command using the `script` option.

  If present, the option’s value is passed to the user’s default shell for
  execution. Any expansion is left to the shell.

- Specify any script using the `script` option, starting with a `#!` line.

  If the script option is present and (after removing leading whitespace) starts
  with `#!`, the script is written to a temporary file which is then made
  executable and executed. After finishing, the temporary file is deleted.

If the command creates any files or directories, they should be reported. There
are two alternative ways to do that:

- using the option `creates` (list of paths) and explicitly listing the files.
- having the script output the paths to stdout, separated by newlines.

#### install-script

`install-script` actions are run during installation of the project, in the
normal workflow.

#### uninstall-script

This is run during uninstallation, _before_ the project’s files are deleted.

#### post-uninstall-script

… is run during uninstallation, _after_ the project’s files have been deleted.

## Configurators

A configurator is an interactive routine that at the same time creates a
configuration and runs the installation steps.
