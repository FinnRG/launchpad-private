# Copyright 2010-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Unit tests for the string TALES formatter."""

import unittest
from doctest import DocTestSuite
from textwrap import dedent

from testtools.matchers import Equals, Matcher
from zope.component import getUtility

from lp.app.browser.stringformatter import (
    FormattersAPI,
    linkify_bug_numbers,
    linkify_diff_file_header,
    parse_diff,
)
from lp.services.config import config
from lp.services.features.testing import MemoryFeatureFixture
from lp.services.webapp.interfaces import ILaunchBag
from lp.services.webapp.publisher import canonical_url
from lp.testing import TestCase, TestCaseWithFactory
from lp.testing.layers import BaseLayer, DatabaseFunctionalLayer, ZopelessLayer
from lp.testing.pages import find_tags_by_class


def test_split_paragraphs():
    r"""
    The split_paragraphs() method is used to split a block of text
    into paragraphs, which are separated by one or more blank lines.
    Paragraphs are yielded as a list of lines in the paragraph.

      >>> from lp.app.browser.stringformatter import split_paragraphs
      >>> for paragraph in split_paragraphs("\na\nb\n\nc\nd\n\n\n"):
      ...     print(paragraph)
      ...
      ['a', 'b']
      ['c', 'd']
    """


def test_re_substitute():
    """
    When formatting text, we want to replace portions with links.
    re.sub() works fairly well for this, but doesn't give us much
    control over the non-matched text.  The re_substitute() function
    lets us do that.

      >>> import re
      >>> from lp.app.browser.stringformatter import re_substitute

      >>> def match_func(match):
      ...     return "[%s]" % match.group()
      ...
      >>> def nomatch_func(text):
      ...     return "{%s}" % text
      ...

      >>> pat = re.compile("a{2,6}")
      >>> print(
      ...     re_substitute(
      ...         pat, match_func, nomatch_func, "bbaaaabbbbaaaaaaa aaaaaaaab"
      ...     )
      ... )
      {bb}[aaaa]{bbbb}[aaaaaa]{a }[aaaaaa][aa]{b}
    """


def test_add_word_breaks():
    """
    Long words can cause page layout problems, so we insert manual
    word breaks into long words.  Breaks are added at least once every
    15 characters, but will break on as little as 7 characters if
    there is a suitable non-alphanumeric character to break after.

      >>> from lp.app.browser.stringformatter import add_word_breaks

      >>> print(add_word_breaks("abcdefghijklmnop"))
      abcdefghijklmno<wbr />p

      >>> print(add_word_breaks("abcdef/ghijklmnop"))
      abcdef/<wbr />ghijklmnop

      >>> print(add_word_breaks("ab/cdefghijklmnop"))
      ab/cdefghijklmn<wbr />op

    The string can contain HTML entities, which do not get split:

      >>> print(add_word_breaks("abcdef&anentity;hijklmnop"))
      abcdef&anentity;<wbr />hijklmnop
    """


def test_break_long_words():
    """
    If we have a long HTML string, break_long_words() can be used to
    add word breaks to the long words.  It will not add breaks inside HTML
    tags.  Only words longer than 20 characters will have breaks added.

      >>> from lp.app.browser.stringformatter import break_long_words

      >>> print(break_long_words("1234567890123456"))
      1234567890123456

      >>> print(break_long_words("12345678901234567890"))
      123456789012345<wbr />67890

      >>> print(break_long_words('<tag a12345678901234567890="foo"></tag>'))
      <tag a12345678901234567890="foo"></tag>

      >>> print(
      ...     break_long_words("12345678901234567890 1234567890.1234567890")
      ... )
      123456789012345<wbr />67890 1234567890.<wbr />1234567890

      >>> print(break_long_words("1234567890&abcdefghi;123"))
      1234567890&abcdefghi;123

      >>> print(break_long_words("<tag>1234567890123456</tag>"))
      <tag>1234567890123456</tag>
    """


class TestLinkifyingBugs(TestCase):
    def test_regular_bug_case_works(self):
        test_strings = [
            "bug 34434",
            "bugnumber 34434",
            "bug number 34434",
        ]
        expected_html = [
            '<p><a href="/bugs/34434" ' 'class="bug-link">bug 34434</a></p>',
            '<p><a href="/bugs/34434" '
            'class="bug-link">bugnumber 34434</a></p>',
            '<p><a href="/bugs/34434" '
            'class="bug-link">bug number 34434</a></p>',
        ]
        self.assertEqual(
            expected_html,
            [FormattersAPI(text).text_to_html() for text in test_strings],
        )

    def test_things_do_not_link_if_they_should_not(self):
        test_strings = [
            "bugnumber.4",
            "bug number.4",
            "bugno.4",
            "bug no.4",
        ]
        expected_html = [
            "<p>bugnumber.4</p>",
            "<p>bug number.4</p>",
            "<p>bugno.4</p>",
            "<p>bug no.4</p>",
        ]
        self.assertEqual(
            expected_html,
            [FormattersAPI(text).text_to_html() for text in test_strings],
        )

    def test_explicit_bug_linkification(self):
        text = "LP: #10"
        self.assertEqual(
            'LP: <a href="/bugs/10" class="bug-link">#10</a>',
            linkify_bug_numbers(text),
        )


class TestLinkifyingProtocols(TestCaseWithFactory):
    layer = DatabaseFunctionalLayer

    def test_normal_set(self):
        test_strings = [
            "http://example.com",
            "http://example.com/",
            "http://example.com/path",
            "http://example.com/path/",
        ]

        expected_strings = [
            (
                '<p><a rel="nofollow" href="http://example.com">'
                "http://<wbr />example.<wbr />com</a></p>"
            ),
            (
                '<p><a rel="nofollow" href="http://example.com/">'
                "http://<wbr />example.<wbr />com/</a></p>"
            ),
            (
                '<p><a rel="nofollow" href="http://example.com/path">'
                "http://<wbr />example.<wbr />com/path</a></p>"
            ),
            (
                '<p><a rel="nofollow" href="http://example.com/path/">'
                "http://<wbr />example.<wbr />com/path/</a></p>"
            ),
        ]

        self.assertEqual(
            expected_strings,
            [FormattersAPI(text).text_to_html() for text in test_strings],
        )

    def test_parens_handled_well(self):
        test_strings = [
            "(http://example.com)",
            "http://example.com/path_(with_parens)",
            "(http://example.com/path_(with_parens))",
            "(http://example.com/path_(with_parens)and_stuff)",
            "http://example.com/path_(with_parens",
        ]

        expected_html = [
            (
                '<p>(<a rel="nofollow" href="http://example.com">'
                "http://<wbr />example.<wbr />com</a>)</p>"
            ),
            (
                '<p><a rel="nofollow" '
                'href="http://example.com/path_(with_parens)">'
                "http://<wbr />example.<wbr />com/path_"
                "<wbr />(with_parens)</a></p>"
            ),
            (
                '<p>(<a rel="nofollow" '
                'href="http://example.com/path_(with_parens)">'
                "http://<wbr />example.<wbr />com/path_"
                "<wbr />(with_parens)</a>)</p>"
            ),
            (
                '<p>(<a rel="nofollow" '
                'href="http://example.com/path_(with_parens)and_stuff">'
                "http://<wbr />example.<wbr />com"
                "/path_<wbr />(with_parens)<wbr />and_stuff</a>)</p>"
            ),
            (
                '<p><a rel="nofollow" '
                'href="http://example.com/path_(with_parens">'
                "http://<wbr />example.<wbr />com"
                "/path_<wbr />(with_parens</a></p>"
            ),
        ]

        self.assertEqual(
            expected_html,
            [FormattersAPI(text).text_to_html() for text in test_strings],
        )

    def test_protocol_alone_does_not_link(self):
        test_string = "This doesn't link: apt:"
        html = FormattersAPI(test_string).text_to_html()
        expected_html = "<p>This doesn&#x27;t link: apt:</p>"
        self.assertEqual(expected_html, html)

        test_string = "This doesn't link: http://"
        html = FormattersAPI(test_string).text_to_html()
        expected_html = "<p>This doesn&#x27;t link: http://</p>"
        self.assertEqual(expected_html, html)

    def test_apt_is_linked(self):
        test_string = "This becomes a link: apt:some-package"
        html = FormattersAPI(test_string).text_to_html()
        expected_html = (
            "<p>This becomes a link: "
            '<a rel="nofollow" '
            'href="apt:some-package">apt:some-<wbr />package</a></p>'
        )
        self.assertEqual(expected_html, html)

        # Do it again for apt://
        test_string = "This becomes a link: apt://some-package"
        html = FormattersAPI(test_string).text_to_html()
        expected_html = (
            "<p>This becomes a link: "
            '<a rel="nofollow" '
            'href="apt://some-package">apt://some-<wbr />package</a></p>'
        )
        self.assertEqual(expected_html, html)

    def test_file_is_not_linked(self):
        test_string = "This doesn't become a link: file://some/file.txt"
        html = FormattersAPI(test_string).text_to_html()
        expected_html = (
            "<p>This doesn&#x27;t become a link: "
            "file://<wbr />some/file.<wbr />txt</p>"
        )
        self.assertEqual(expected_html, html)

    def test_no_link_with_linkify_text_false(self):
        test_string = "This doesn't become a link: http://www.example.com/"
        html = FormattersAPI(test_string).text_to_html(linkify_text=False)
        expected_html = (
            "<p>This doesn&#x27;t become a link: http://www.example.com/</p>"
        )
        self.assertEqual(expected_html, html)

    def test_no_link_html_code_with_linkify_text_false(self):
        test_string = '<a href="http://example.com/">http://example.com/</a>'
        html = FormattersAPI(test_string).text_to_html(linkify_text=False)
        expected_html = (
            "<p>&lt;a href=&quot;http://example.com/&quot;&gt;"
            "http://example.com/&lt;/a&gt;</p>"
        )
        self.assertEqual(expected_html, html)

    def test_double_email_in_linkify_email(self):
        person = self.factory.makePerson(email="foo@example.org")
        test_string = (
            " * Foo. &lt;foo@example.org&gt;\n * Bar &lt;foo@example.org&gt;"
        )
        html = FormattersAPI(test_string).linkify_email()
        url = canonical_url(person)
        expected_html = (
            ' * Foo. &lt;<a href="%s" class="sprite person">foo@example.org'
            '</a>&gt;\n * Bar &lt;<a href="%s" class="sprite person">'
            "foo@example.org</a>&gt;" % (url, url)
        )
        self.assertEqual(expected_html, html)


class TestLastParagraphClass(TestCase):
    def test_last_paragraph_class(self):
        self.assertEqual(
            '<p>Foo</p>\n<p class="last">Bar</p>',
            FormattersAPI("Foo\n\nBar").text_to_html(
                last_paragraph_class="last"
            ),
        )


class TestParseDiff(TestCase):
    """Test the parser for fmt:diff and fmt:ssdiff."""

    def test_emptyString(self):
        # An empty string yields no lines.
        self.assertEqual([], list(parse_diff("")))

    def test_almostEmptyString(self):
        # White space yields a single line of text.
        self.assertEqual([("text", 1, 0, 0, " ")], list(parse_diff(" ")))

    def test_unicode(self):
        # Diffs containing Unicode work too.
        self.assertEqual(
            [("text", 1, 0, 0, "Unicode \u1010")],
            list(parse_diff("Unicode \u1010")),
        )

    def assertParses(self, expected, diff):
        diff_lines = diff.splitlines()
        self.assertEqual(
            [
                (css_class, row + 1, orig_row, mod_row, diff_lines[row])
                for row, (css_class, orig_row, mod_row) in enumerate(expected)
            ],
            list(parse_diff(diff)),
        )

    def test_basic_bzr(self):
        # A basic Bazaar diff with a few different classes is parsed correctly.
        diff = dedent(
            """\
            === modified file 'tales.py'
            --- tales.py
            +++ tales.py
            @@ -2435,7 +2439,8 @@
                 def method(self):
            -        removed this line
            +        added this line
            +        added another line
                     something in between
            -------- a sql style comment
            ++++++++ a line of pluses
            ########
            # A merge directive comment.
            """
        )
        expected = [
            ("diff-file text", None, None),
            ("diff-header text", None, None),
            ("diff-header text", None, None),
            ("diff-chunk text", None, None),
            ("text", 2435, 2439),
            ("diff-removed text", 2436, None),
            ("diff-added text", None, 2440),
            ("diff-added text", None, 2441),
            ("text", 2437, 2442),
            ("diff-removed text", 2438, None),
            ("diff-added text", None, 2443),
            ("diff-comment text", None, None),
            ("diff-comment text", None, None),
        ]
        self.assertParses(expected, diff)

    def test_basic_git(self):
        # A basic Git diff with a few different classes is parsed correctly.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            index aaaaaaa..bbbbbbb 100644
            --- a/tales.py
            +++ b/tales.py
            @@ -2435,7 +2439,8 @@
                 def method(self):
            -        removed this line
            +        added this line
            +        added another line
                     something in between
            -------- a sql style comment
            ++++++++ a line of pluses
            """
        )
        expected = [
            ("diff-file text", None, None),
            ("diff-file text", None, None),
            ("diff-header text", None, None),
            ("diff-header text", None, None),
            ("diff-chunk text", None, None),
            ("text", 2435, 2439),
            ("diff-removed text", 2436, None),
            ("diff-added text", None, 2440),
            ("diff-added text", None, 2441),
            ("text", 2437, 2442),
            ("diff-removed text", 2438, None),
            ("diff-added text", None, 2443),
        ]
        self.assertParses(expected, diff)

    def test_config_value_limits_line_count(self):
        # The config.diff.max_line_format contains the maximum number of
        # lines to parse.
        diff = dedent(
            """\
            === modified file 'tales.py'
            --- tales.py
            +++ tales.py
            @@ -2435,6 +2435,8 @@
                 def method(self):
            -        removed this line
            +        added this line
            ########
            # A merge directive comment.
            """
        )
        expected = [
            ("diff-file text", None, None),
            ("diff-header text", None, None),
            ("diff-header text", None, None),
        ]
        self.pushConfig("diff", max_format_lines=3)
        self.assertParses(expected, diff)

    def test_multiple_hunks(self):
        # Diffs containing multiple hunks are parsed reasonably, and the
        # original and modified row numbers are adjusted for each hunk.
        diff = dedent(
            """\
            @@ -2,2 +2,3 @@
             a
            -b
            +c
            +d
            @@ -10,3 +11,2 @@
            -e
             f
            -g
            +h
            """
        )
        expected = [
            ("diff-chunk text", None, None),
            ("text", 2, 2),
            ("diff-removed text", 3, None),
            ("diff-added text", None, 3),
            ("diff-added text", None, 4),
            ("diff-chunk text", None, None),
            ("diff-removed text", 10, None),
            ("text", 11, 11),
            ("diff-removed text", 12, None),
            ("diff-added text", None, 12),
        ]
        self.assertParses(expected, diff)


class TestParseDiffErrors(TestCaseWithFactory):
    layer = ZopelessLayer

    def assertParses(self, expected, diff):
        diff_lines = diff.splitlines()
        self.assertEqual(
            [
                (css_class, row + 1, orig_row, mod_row, diff_lines[row])
                for row, (css_class, orig_row, mod_row) in enumerate(expected)
            ],
            list(parse_diff(diff)),
        )

    def test_bad_hunk_header(self):
        # A bad hunk header causes the parser to record an OOPS but continue
        # anyway.
        diff = dedent(
            """\
            @@ some nonsense @@
                 def method(self):
            """
        )
        expected = [
            ("diff-chunk text", None, None),
            ("text", 1, 1),
        ]
        self.assertParses(expected, diff)
        self.assertEqual("MalformedHunkHeader", self.oopses[0]["type"])


class TestDiffFormatter(TestCase):
    """Test the string formatter fmt:diff."""

    def test_emptyString(self):
        # An empty string gives an empty string.
        self.assertEqual("", FormattersAPI("").format_diff())

    def test_almostEmptyString(self):
        # White space doesn't count as empty, and is formatted.
        self.assertEqual(
            '<table class="diff unidiff"><tr id="diff-line-1">'
            '<td class="line-no unselectable">1</td><td class="text"> '
            "</td></tr></table>",
            FormattersAPI(" ").format_diff(),
        )

    def test_format_unicode(self):
        # Sometimes the strings contain unicode, those should work too.
        self.assertEqual(
            '<table class="diff unidiff"><tr id="diff-line-1">'
            '<td class="line-no unselectable">1</td><td class="text">'
            "Unicode \u1010</td></tr></table>",
            FormattersAPI("Unicode \u1010").format_diff(),
        )

    def test_cssClasses(self):
        # Different parts of the diff have different css classes.
        diff = dedent(
            """\
            === modified file 'tales.py'
            --- tales.py
            +++ tales.py
            @@ -2435,6 +2435,8 @@
                 def format_diff(self):
            -        removed this line
            +        added this line
            -------- a sql style comment
            ++++++++ a line of pluses
            ########
            # A merge directive comment.
            """
        )
        html = FormattersAPI(diff).format_diff()
        line_numbers = find_tags_by_class(html, "line-no")
        self.assertEqual(
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"],
            [tag.decode_contents() for tag in line_numbers],
        )
        text = find_tags_by_class(html, "text")
        self.assertEqual(
            [
                ["diff-file", "text"],
                ["diff-header", "text"],
                ["diff-header", "text"],
                ["diff-chunk", "text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-comment", "text"],
                ["diff-comment", "text"],
            ],
            [tag["class"] for tag in text],
        )

    def test_cssClasses_git(self):
        # Git diffs look slightly different, so check that they also end up
        # with the correct CSS classes.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            index aaaaaaa..bbbbbbb 100644
            --- a/tales.py
            +++ b/tales.py
            @@ -2435,6 +2435,8 @@
                 def format_diff(self):
            -        removed this line
            +        added this line
            -------- a sql style comment
            ++++++++ a line of pluses
            """
        )
        html = FormattersAPI(diff).format_diff()
        line_numbers = find_tags_by_class(html, "line-no")
        self.assertEqual(
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"],
            [tag.decode_contents() for tag in line_numbers],
        )
        text = find_tags_by_class(html, "text")
        self.assertEqual(
            [
                ["diff-file", "text"],
                ["diff-file", "text"],
                ["diff-header", "text"],
                ["diff-header", "text"],
                ["diff-chunk", "text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
            ],
            [tag["class"] for tag in text],
        )

    def test_get_file_url_links_headers(self):
        # When a get_file_url callback is supplied, the "---"/"+++" file
        # headers of a Git diff link the path to the returned URL.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            index aaaaaaa..bbbbbbb 100644
            --- a/tales.py
            +++ b/tales.py
            @@ -1,1 +1,1 @@
            -removed
            +added
            """
        )

        def get_file_url(path, side):
            return "https://git.example.test/%s/tree/%s" % (side, path)

        html = FormattersAPI(diff).format_diff(get_file_url=get_file_url)
        headers = find_tags_by_class(html, "diff-header")
        links = [header.find("a") for header in headers]
        self.assertEqual(
            [
                "https://git.example.test/a/tree/tales.py",
                "https://git.example.test/b/tree/tales.py",
            ],
            [link["href"] for link in links],
        )
        self.assertEqual(
            ["a/tales.py", "b/tales.py"],
            [link.decode_contents() for link in links],
        )
        self.assertEqual(
            ["diff-file-link"], [link["class"] for link in links][0]
        )
        self.assertEqual([], find_tags_by_class(html, "diff-chunk")[0]("a"))

    def test_get_file_url_default_unchanged(self):
        # Without a callback (the default) the output is unchanged, so all
        # other fmt:diff callers are unaffected.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            --- a/tales.py
            +++ b/tales.py
            @@ -1,1 +1,1 @@
            -removed
            +added
            """
        )
        self.assertEqual(
            FormattersAPI(diff).format_diff(),
            FormattersAPI(diff).format_diff(
                get_file_url=lambda path, side: None
            ),
        )

    def test_get_file_url_dev_null_not_linked(self):
        # Added or deleted files use /dev/null on one side, which is not
        # linked.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            new file mode 100644
            index 0000000..bbbbbbb
            --- /dev/null
            +++ b/tales.py
            @@ -0,0 +1,1 @@
            +added
            """
        )

        def get_file_url(path, side):
            return "https://git.example.test/%s" % path

        html = FormattersAPI(diff).format_diff(get_file_url=get_file_url)
        headers = find_tags_by_class(html, "diff-header")
        hrefs = [
            header.find("a")["href"] if header.find("a") else None
            for header in headers
        ]
        self.assertEqual([None, "https://git.example.test/tales.py"], hrefs)


class TestSideBySideDiffFormatter(TestCase):
    """Test the string formatter fmt:ssdiff."""

    def test_emptyString(self):
        # An empty string gives an empty string.
        self.assertEqual("", FormattersAPI("").format_ssdiff())

    def test_almostEmptyString(self):
        # White space doesn't count as empty, and is formatted.
        self.assertEqual(
            '<table class="diff ssdiff"><tr id="diff-line-1">'
            '<td class="line-no unselectable" style="display: none">1</td>'
            '<td class="ss-line-no unselectable">0</td>'
            '<td class="text"></td>'
            '<td class="ss-line-no unselectable">0</td>'
            '<td class="text"></td>'
            "</tr></table>",
            FormattersAPI(" ").format_ssdiff(),
        )

    def test_format_unicode(self):
        # Sometimes the strings contain unicode, those should work too.
        self.assertEqual(
            '<table class="diff ssdiff"><tr id="diff-line-1">'
            '<td class="line-no unselectable" style="display: none">1</td>'
            '<td class="ss-line-no unselectable">0</td>'
            '<td class="text">Unicode \u1010</td>'
            '<td class="ss-line-no unselectable">0</td>'
            '<td class="text">Unicode \u1010</td>'
            "</tr></table>",
            FormattersAPI("Unicode \u1010").format_ssdiff(),
        )

    def test_cssClasses(self):
        # Different parts of the diff have different css classes.
        diff = dedent(
            """\
            === modified file 'tales.py'
            --- tales.py
            +++ tales.py
            @@ -2435,7 +2439,8 @@
                 def format_ssdiff(self):
            -        removed this line
            +        added this line
            +        added another line
                     something in between
            -------- a sql style comment
            ++++++++ a line of pluses
            ########
            # A merge directive comment.
            """
        )
        html = FormattersAPI(diff).format_ssdiff()
        line_numbers = find_tags_by_class(html, "line-no")
        self.assertEqual(
            ["1", "2", "3", "4", "5", "7", "8", "9", "11", "12", "13"],
            [tag.decode_contents() for tag in line_numbers],
        )
        ss_line_numbers = find_tags_by_class(html, "ss-line-no")
        self.assertEqual(
            [
                "2435",
                "2439",
                "2436",
                "2440",
                "",
                "2441",
                "2437",
                "2442",
                "2438",
                "2443",
            ],
            [tag.decode_contents() for tag in ss_line_numbers],
        )
        text = find_tags_by_class(html, "text")
        self.assertEqual(
            [
                ["diff-file", "text"],
                ["diff-header", "text"],
                ["diff-header", "text"],
                ["diff-chunk", "text"],
                ["text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-comment", "text"],
                ["diff-comment", "text"],
            ],
            [tag["class"] for tag in text],
        )

    def test_cssClasses_git(self):
        # Git diffs look slightly different, so check that they also end up
        # with the correct CSS classes.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            index aaaaaaa..bbbbbbb 100644
            --- a/tales.py
            +++ b/tales.py
            @@ -2435,7 +2439,8 @@
                 def format_ssdiff(self):
            -        removed this line
            +        added this line
            +        added another line
                     something in between
            -------- a sql style comment
            ++++++++ a line of pluses
            """
        )
        html = FormattersAPI(diff).format_ssdiff()
        line_numbers = find_tags_by_class(html, "line-no")
        self.assertEqual(
            ["1", "2", "3", "4", "5", "6", "8", "9", "10", "12"],
            [tag.decode_contents() for tag in line_numbers],
        )
        ss_line_numbers = find_tags_by_class(html, "ss-line-no")
        self.assertEqual(
            [
                "2435",
                "2439",
                "2436",
                "2440",
                "",
                "2441",
                "2437",
                "2442",
                "2438",
                "2443",
            ],
            [tag.decode_contents() for tag in ss_line_numbers],
        )
        text = find_tags_by_class(html, "text")
        self.assertEqual(
            [
                ["diff-file", "text"],
                ["diff-file", "text"],
                ["diff-header", "text"],
                ["diff-header", "text"],
                ["diff-chunk", "text"],
                ["text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
                ["text"],
                ["text"],
                ["diff-removed", "text"],
                ["diff-added", "text"],
            ],
            [tag["class"] for tag in text],
        )

    def test_get_file_url_links_headers(self):
        # The side-by-side formatter also links Git file headers when a
        # get_file_url callback is supplied.
        diff = dedent(
            """\
            diff --git a/tales.py b/tales.py
            index aaaaaaa..bbbbbbb 100644
            --- a/tales.py
            +++ b/tales.py
            @@ -1,1 +1,1 @@
            -removed
            +added
            """
        )

        def get_file_url(path, side):
            return "https://git.example.test/%s/tree/%s" % (side, path)

        html = FormattersAPI(diff).format_ssdiff(get_file_url=get_file_url)
        headers = find_tags_by_class(html, "diff-header")
        links = [header.find("a") for header in headers]
        self.assertEqual(
            [
                "https://git.example.test/a/tree/tales.py",
                "https://git.example.test/b/tree/tales.py",
            ],
            [link["href"] for link in links],
        )


class TestLinkifyDiffFileHeader(TestCase):
    """Test the linkify_diff_file_header helper directly."""

    def _url(self, path, side):
        return "https://git.example.test/%s/tree/%s" % (side, path)

    def test_no_callback_returns_line(self):
        line = "--- a/tales.py"
        self.assertIs(
            line, linkify_diff_file_header("diff-header text", line, None)
        )

    def test_non_header_returns_line(self):
        # Content lines that happen to start with "-" are not file headers.
        line = "-removed a line"
        self.assertIs(
            line,
            linkify_diff_file_header("diff-removed text", line, self._url),
        )

    def test_old_side_uses_a(self):
        result = linkify_diff_file_header(
            "diff-header text", "--- a/dir/tales.py", self._url
        )
        self.assertEqual(
            '--- <a class="diff-file-link" '
            'href="https://git.example.test/a/tree/dir/tales.py">'
            "a/dir/tales.py</a>",
            result.escapedtext,
        )

    def test_new_side_uses_b(self):
        result = linkify_diff_file_header(
            "diff-header text", "+++ b/tales.py", self._url
        )
        self.assertEqual(
            '+++ <a class="diff-file-link" '
            'href="https://git.example.test/b/tree/tales.py">'
            "b/tales.py</a>",
            result.escapedtext,
        )

    def test_dev_null_returns_line(self):
        line = "--- /dev/null"
        self.assertIs(
            line,
            linkify_diff_file_header("diff-header text", line, self._url),
        )

    def test_none_url_returns_line(self):
        line = "--- a/tales.py"
        self.assertIs(
            line,
            linkify_diff_file_header(
                "diff-header text", line, lambda path, side: None
            ),
        )

    def test_path_and_url_are_escaped(self):
        result = linkify_diff_file_header(
            "diff-header text",
            '+++ b/a&b".py',
            lambda path, side: "https://git.example.test/x?p=%s" % path,
        )
        self.assertNotIn("&b\"", result.escapedtext)
        self.assertIn("&amp;", result.escapedtext)
        self.assertIn("&quot;", result.escapedtext)


class TestOOPSFormatter(TestCase):
    """A test case for the oops_id() string formatter."""

    layer = DatabaseFunctionalLayer

    def _setDeveloper(self, value):
        """Override ILaunchBag.developer for testing purposes."""
        launch_bag = getUtility(ILaunchBag)
        self.addCleanup(launch_bag.setDeveloper, launch_bag.developer)
        launch_bag.setDeveloper(value)

    def test_doesnt_linkify_for_non_developers(self):
        # OOPS IDs won't be linkified for non-developers.
        oops_id = "OOPS-12345TEST"
        formatter = FormattersAPI(oops_id)
        formatted_string = formatter.oops_id()

        self.assertEqual(
            oops_id,
            formatted_string,
            "Formatted string should be '%s', was '%s'"
            % (oops_id, formatted_string),
        )

    def test_linkifies_for_developers(self):
        # OOPS IDs will be linkified for Launchpad developers.
        oops_id = "OOPS-12345TEST"
        formatter = FormattersAPI(oops_id)
        self._setDeveloper(True)
        formatted_string = formatter.oops_id()

        expected_string = '<a href="%s">%s</a>' % (
            config.launchpad.oops_root_url + oops_id,
            oops_id,
        )

        self.assertEqual(
            expected_string,
            formatted_string,
            "Formatted string should be '%s', was '%s'"
            % (expected_string, formatted_string),
        )


class MarksDownAs(Matcher):
    def __init__(self, expected_html):
        self.expected_html = expected_html

    def __str__(self):
        return f"MarksDownAs({self.expected_html!r})"

    def match(self, input_string):
        return Equals(self.expected_html).match(
            FormattersAPI(input_string).markdown()
        )


class TestMarkdownDisabled(TestCase):
    """Feature flag can turn Markdown stuff off."""

    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.useFixture(MemoryFeatureFixture({"markdown.enabled": None}))

    def test_plain_text(self):
        self.assertThat(
            "hello **simple** world",
            MarksDownAs("<p>hello **simple** world</p>"),
        )


class TestMarkdown(TestCase):
    """Test for Markdown integration within Launchpad.

    Not an exhaustive test, more of a check for our integration and
    configuration.
    """

    layer = BaseLayer

    def setUp(self):
        super().setUp()
        self.useFixture(MemoryFeatureFixture({"markdown.enabled": "on"}))

    def test_plain_text(self):
        self.assertThat("hello world", MarksDownAs("<p>hello world</p>"))

    def test_fenced_code(self):
        self.assertThat(
            "```\nfoo = 1\n```\n",
            MarksDownAs("<pre><code>foo = 1\n</code></pre>"),
        )

    def test_nl2br(self):
        self.assertThat(
            "hello\nworld\n\nanother paragraph\n",
            MarksDownAs("<p>hello<br>\nworld</p>\n<p>another paragraph</p>"),
        )

    def test_tables(self):
        self.assertThat(
            dedent(
                """\
                Column 1 | Column 2
                -------- | --------
                Cell 1   | Cell 2
                """
            ),
            MarksDownAs(
                dedent(
                    """\
                    <table>
                    <thead>
                    <tr>
                    <th>Column 1</th>
                    <th>Column 2</th>
                    </tr>
                    </thead>
                    <tbody>
                    <tr>
                    <td>Cell 1</td>
                    <td>Cell 2</td>
                    </tr>
                    </tbody>
                    </table>"""
                )
            ),
        )

    def test_escapes_html(self):
        self.assertThat(
            "<script>alert('XSS')</script>",
            MarksDownAs("&lt;script&gt;alert('XSS')&lt;/script&gt;"),
        )


def test_suite():
    suite = unittest.TestSuite()
    suite.addTests(DocTestSuite())
    suite.addTests(unittest.TestLoader().loadTestsFromName(__name__))
    return suite
