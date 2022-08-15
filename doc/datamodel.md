## Release

A release represents a specific version of a project. It has a type (currently, always 'GitHub Release') and the following properties:

| Property | type | for gh-r   | Description                                           |
|----------|------|------------|-------------------------------------------------------|
| id       | str  | url        | Some arbitrary internal ID identifying the release    |
| version  | str  | tag_name   | version number or description                         |
| name     | str  | name       | (optional) label for the user                         |
| date     | date | created_at | sort and comparison key for the version               |
| pre      | bool | prerelease | if true, its a prerelease (not considered for latest) |
| latest   | bool |            | if true, this is the latest release                   |
| assets   | list | assets     | files to download                                     |


