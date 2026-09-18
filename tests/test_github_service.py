from app.github_service import extract_issue_refs


def test_extract_single_issue_ref():
    text = "Can someone look at https://github.com/octocat/Hello-World/issues/42 ?"
    refs = extract_issue_refs(text)
    assert len(refs) == 1
    assert refs[0].owner == "octocat"
    assert refs[0].repo == "Hello-World"
    assert refs[0].issue_number == 42


def test_extract_multiple_issue_refs():
    text = (
        "Two bugs today: https://github.com/foo/bar/issues/1 and "
        "https://github.com/foo/baz/issues/99"
    )
    refs = extract_issue_refs(text)
    assert len(refs) == 2
    assert {r.issue_number for r in refs} == {1, 99}


def test_no_issue_refs_in_plain_text():
    assert extract_issue_refs("just chatting, no links here") == []


def test_ignores_non_issue_github_urls():
    text = "See https://github.com/foo/bar/pull/5 and https://github.com/foo/bar"
    assert extract_issue_refs(text) == []
