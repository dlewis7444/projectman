import re

from terminal_links import URI_SCHEME_PATTERN, uri_scheme


def _matches(text):
    return [m.group(0) for m in re.finditer(URI_SCHEME_PATTERN, text)]


def test_generic_scheme_links_match():
    text = (
        'open https://example.com/a and file:///tmp/x, then '
        'vivaldi://settings/keyboard or chrome-extension://abc@def/page'
    )
    assert _matches(text) == [
        'https://example.com/a',
        'file:///tmp/x,',
        'vivaldi://settings/keyboard',
        'chrome-extension://abc@def/page',
    ]


def test_scheme_characters_match():
    assert _matches('custom+v1.2-beta://host/path') == [
        'custom+v1.2-beta://host/path'
    ]


def test_plain_paths_and_single_colon_text_do_not_match():
    assert _matches('/tmp/file Note: this is not a URI') == []


def test_scheme_must_start_with_a_letter():
    assert _matches('1foo://nope foo://yes') == ['foo://yes']


def test_uri_scheme_is_lowercase():
    assert uri_scheme('Vivaldi://settings/keyboard') == 'vivaldi'
    assert uri_scheme('chrome-extension://abc/page') == 'chrome-extension'


def test_uri_scheme_rejects_non_scheme_uris():
    assert uri_scheme('/tmp/file') is None
    assert uri_scheme('mailto:user@example.com') is None
