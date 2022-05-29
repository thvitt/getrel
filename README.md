An automatic downloader for GitHub releases.

## Interactive use

1.  `ghr-get [--pre] [--latest] [https://github.com/]user/repo`
2. unless `--latest` given, show releases for selection
3. show available assets for the release for selection
4. download
5. optionally unpack

## Automated use

For all 'remembered' releases,

1. check if a new version exists
2. if so, identify a 'matching' asset using a match rule
3. download it
4. unpack it to a configurable directory
5. optionally post-processing, like symlinking binaries etc.

## Configuration use

- allow to 'save' settings from interactive use
