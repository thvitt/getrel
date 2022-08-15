from ghr_get.ghr import GitHubProject


def test_ghr_project_short():
    project = GitHubProject('rgburke/grv')
    assert project.name == 'grv'
    assert project.url == 'https://github.com/rgburke/grv'


def test_ghr_project_full():
    project = GitHubProject('https://github.com/rgburke/grv')
    assert project.name == 'grv'
    assert project.url == 'https://github.com/rgburke/grv'


def test_ghr_project_subdir():
    project = GitHubProject('https://github.com/rgburke/grv/releases/tag/v0.3.2')
    assert project.name == 'grv'
    assert project.url == 'https://github.com/rgburke/grv'

