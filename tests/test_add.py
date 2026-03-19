import pytest
from getrel.add import identifying_pattern, mask_architecture, mask_version
from getrel.actions import Settings

def test_mask_version():
    assert mask_version("foo-1.2.3-linux.tar.gz", "1.2.3") == "foo-*-linux.tar.gz"
    assert mask_version("foo-v1.2.3-linux.tar.gz", "1.2.3") == "foo-*-linux.tar.gz"
    assert mask_version("foo-V1.2.3-linux.tar.gz", "1.2.3") == "foo-*-linux.tar.gz"

def test_mask_architecture():
    settings = Settings(architectures={"x86_64": ["amd64", "x64"]})
    # Mock platform.machine to return x86_64
    import platform
    original_machine = platform.machine
    platform.machine = lambda: "x86_64"
    try:
        assert mask_architecture("foo-x86_64-linux.tar.gz", settings) == "foo-{arch}-linux.tar.gz"
        assert mask_architecture("foo-amd64-linux.tar.gz", settings) == "foo-{arch}-linux.tar.gz"
        assert mask_architecture("foo-x64-linux.tar.gz", settings) == "foo-{arch}-linux.tar.gz"
    finally:
        platform.machine = original_machine

def test_identifying_pattern():
    alternatives = ["foo-windows-x86_64.zip", "foo-macos-x86_64.tar.gz"]
    selection = "foo-linux-x86_64.tar.gz"
    
    # Without version/settings
    assert identifying_pattern(alternatives, selection) == "*l*"
    
    # With version
    assert identifying_pattern(alternatives, selection, version="1.2.3") == "*l*" # version not in selection
    
    selection_v = "foo-1.2.3-linux-x86_64.tar.gz"
    assert identifying_pattern(alternatives, selection_v, version="1.2.3") == "foo-*-linux-x86_64.tar.gz"
    
    # With settings
    settings = Settings(architectures={"x86_64": ["amd64"]})
    import platform
    original_machine = platform.machine
    platform.machine = lambda: "x86_64"
    try:
        # If selection has x86_64, it should be replaced by {arch}
        # But wait, identifying_pattern tries to find SHORTEST pattern.
        # "*l*" is shorter than "foo-*-linux-{arch}.tar.gz"

        # Let's try a case where we NEED arch to distinguish
        alternatives_arch = ["foo-linux-arm64.tar.gz"]
        selection_arch = "foo-linux-x86_64.tar.gz"
        # pattern should be full masked string since it's unique
        assert identifying_pattern(alternatives_arch, selection_arch, settings=settings) == "foo-linux-{arch}.tar.gz"

        # Now with version AND settings on selection_v
        selection_v_arch = "foo-1.2.3-linux-x86_64.tar.gz"
        pattern = identifying_pattern(alternatives, selection_v_arch, version="1.2.3", settings=settings)
        assert pattern == "foo-*-linux-{arch}.tar.gz"

    finally:
        platform.machine = original_machine


def test_identifying_pattern_complex():
    settings = Settings(architectures={"x86_64": ["amd64", "x64"]})
    import platform
    original_machine = platform.machine
    platform.machine = lambda: "x86_64"
    try:
        alternatives = ["project-v1.0-win-x64.zip", "project-v1.0-mac-x64.zip"]
        selection = "project-v1.0-linux-x64.zip"
        version = "1.0"
        
        pattern = identifying_pattern(alternatives, selection, version=version, settings=settings)
        assert "{arch}" in pattern
        assert "*" in pattern
        assert "linux" in pattern
        
        # Test that the pattern actually matches
        import fnmatch
        expanded = list(settings.expand_arch(pattern))
        assert any(fnmatch.fnmatch(selection, p) for p in expanded)
        for alt in alternatives:
            assert not any(fnmatch.fnmatch(alt, p) for p in expanded)
            
    finally:
        platform.machine = original_machine
