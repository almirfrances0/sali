from sali.runtime.verbatim import distinctive_tokens, anchor_args

def _fix(user, args):
    return anchor_args(args, distinctive_tokens(user), workspace="sali-works")

def test_corrects_domain_and_query():
    fixed, corr = _fix("search about tanzahost", {"query": "tanzhost owner", "command": "whois tanzhost.com"})
    assert fixed["query"] == "tanzahost owner" and fixed["command"] == "whois tanzahost.com"
    assert len(corr) == 2

def test_www_label_domain():
    fixed, _ = _fix("scrape tanzahost.co.tz", {"url": "https://www.tanzhost.co.tz/about"})
    assert fixed["url"] == "https://www.tanzahost.co.tz/about"

def test_workspace_canon_always():
    fixed, _ = _fix("list my files", {"path": "/home/almir/Desktop/soli-works"})
    assert fixed["path"].endswith("sali-works")

def test_not_context_makes_corruption_correctable():
    fixed, _ = _fix("its tanzahost.com not tanzhost", {"command": "curl tanzhost.com"})
    assert fixed["command"] == "curl tanzahost.com"

def test_leaves_common_words_alone():
    _, corr = _fix("search tanzahost", {"query": "python docker results folder"})
    assert corr == []

def test_leaves_payload_untouched():
    _, corr = _fix("write report", {"content": "the tanzhost data shows..."})
    assert corr == []

def test_distant_token_not_corrected():
    _, corr = _fix("about tanzahost", {"command": "curl https://tanzania.go.tz"})
    assert corr == []
