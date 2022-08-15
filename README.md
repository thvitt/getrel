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


# Use Cases

## Finding a download

### GitHub releases

Release via API:

- latest release (separate endpoint, there is a definition in the API description on how to find this from the list)
- list of releases
- release has a publication date that can be used for the update check

Asset via API:

- There is a list of assets in the API answer, we can select one via glob pattern

### Arbitrary web pages

1. load the web page via lxml and check last modification date / etag.
2. Release Page: XPath expression to a `a[href]` (⇒ load the referenced page) (optional)
3. Download: XPath expression to a `a[href]` (⇒ download the file)


## Postprocessing a download

The downloaded file can be either an archive or directly an executable.

1. We create a directory `~/.local/share/ghr-get/<project>/`. In this directory, the downloaded file is stored plus a `.ghr-get.toml` with metadata (like date etc.)
2. If it is an archive, it is unpacked.
3. If an executable is identified, it is chmod 755
4. we might create symlinks
5. we might run a post-update script
