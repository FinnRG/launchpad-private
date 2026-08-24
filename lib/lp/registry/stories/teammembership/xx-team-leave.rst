Leaving the team
----------------

Foo Bar decided to leave the 'ubuntu-team'. They visit the home page
and chooses the "Leave" link. They confirm that they want to leave the team
by choosing the 'Leave' button.

Leaving a team is a security-sensitive action, since a team's membership
can be used to scope a security-sensitive feature flag (bug #1793647), so
it requires a recently-authenticated ("fresh") login, just like other
account security pages such as +editsshkeys.

    >>> from zope.component import getUtility
    >>> from lp.registry.interfaces.person import IPersonSet
    >>> from lp.testing.pages import setupBrowserFreshLogin

    >>> login(ANONYMOUS)
    >>> foo_bar = getUtility(IPersonSet).getByEmail("foo.bar@canonical.com")
    >>> logout()
    >>> admin_browser = setupBrowserFreshLogin(foo_bar)

    >>> admin_browser.open("http://launchpad.test/~ubuntu-team")
    >>> admin_browser.title
    'Ubuntu Team in Launchpad'

    >>> admin_browser.getLink("Leave").click()
    >>> admin_browser.title
    'Leave Ubuntu Team...
    >>> content = find_main_content(admin_browser.contents)
    >>> print(content.p)
    <p>Are you sure you want to leave this team?</p>
    >>> admin_browser.getControl("Leave").click()

Foo Bar is redirected to the team page after they leave.

    >>> admin_browser.title
    'Ubuntu Team in Launchpad'
    >>> admin_browser.getLink("Join").url
    'http://launchpad.test/~ubuntu-team/+join'


Leaving a private team
----------------------

When someone leaves a private team, they are no longer able to see the team so
are redirected to their personal Launchpad homepage with a suitable message.

    >>> login(ANONYMOUS)
    >>> member = getUtility(IPersonSet).getByEmail("member@canonical.com")
    >>> logout()
    >>> browser = setupBrowserFreshLogin(member)
    >>> browser.open("http://launchpad.test/~myteam")
    >>> browser.title
    'My Team in Launchpad'

    >>> browser.getLink("Leave").click()
    >>> browser.title
    'Leave My Team...

    >>> browser.getControl("Leave").click()

User is redirect to their homepage page after leaving.

    >>> browser.url
    'http://launchpad.test/~member'

    >>> print(
    ...     extract_text(
    ...         first_tag_by_class(browser.contents, "informational message")
    ...     )
    ... )
    You are no longer a member of private team...

Team overview page quick-links
------------------------------

A member of a team can quickly leave a team by using the links on the
team's overview page.

    >>> login(ANONYMOUS)
    >>> carlos = getUtility(IPersonSet).getByEmail("carlos@canonical.com")
    >>> logout()
    >>> browser = setupBrowserFreshLogin(carlos)
    >>> browser.open("http://launchpad.test/~admins")
    >>> print(
    ...     extract_text(find_tag_by_id(browser.contents, "your-involvement"))
    ... )
    You are a member of this team...
    >>> browser.getLink("Leave the Team").click()
    >>> browser.title
    'Leave Launchpad Administrators...
    >>> browser.getControl("Leave").click()
    >>> print(browser.title)
    Launchpad Administrators in Launchpad

    # The 'Leave' link should be gone.
    >>> browser.getLink("Leave the Team")
    Traceback (most recent call last):
    ...
    zope.testbrowser.browser.LinkNotFoundError

    # And the 'Join' link should have returned.
    >>> browser.getLink("Join the team")
    <Link ...>

Team owners do not have the option to leave.

    >>> browser.open("http://launchpad.test/~testing-spanish-team")
    >>> print(
    ...     extract_text(find_tag_by_id(browser.contents, "your-involvement"))
    ... )
    You own this team...
    >>> browser.getLink("Leave the Team")
    Traceback (most recent call last):
    ...
    zope.testbrowser.browser.LinkNotFoundError
