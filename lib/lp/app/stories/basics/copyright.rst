Launchpad has a copyright notice in different templates in the code base.

The tour pages.

    >>> browser.open("http://launchpad.test/")
    >>> browser.getLink("Take the tour").click()
    >>> print(
    ...     extract_text(
    ...         find_tag_by_id(browser.contents, "footer-navigation")
    ...     )
    ... )
    Next...© 2004 Canonical Ltd...

The main template.

    >>> browser.open("http://launchpad.test")
    >>> print(extract_text(find_tag_by_id(browser.contents, "footer")))
    Home
    © 2004 Canonical Ltd.
    ...
