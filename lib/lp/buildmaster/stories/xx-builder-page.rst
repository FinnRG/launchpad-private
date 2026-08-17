Builder page
============

An anonymous user visits the +builds page. They can see a summary of the
builder state. In the sampledata, the builder 'bob' is building
'mozilla-firefox'.

    >>> from zope.component import getUtility
    >>> from lp.buildmaster.interfaces.builder import IBuilderSet

    >>> login("foo.bar@canonical.com")
    >>> bob_builder = getUtility(IBuilderSet)["bob"]
    >>> bob_builder.version = "100"
    >>> logout()

    >>> anon_browser.open("http://launchpad.test/builders")
    >>> anon_browser.getLink("bob").click()

    >>> print(extract_text(find_main_content(anon_browser.contents)))
    Bob The Builder
    ...
    Bob The Builder builds 386 binaries and is owned by Launchpad Buildd
    Admins.
    ...

    >>> print(
    ...     extract_text(
    ...         find_portlet(
    ...             anon_browser.contents, "View full history Current status"
    ...         )
    ...     )
    ... )
    View full history Current status
    Building i386 build of mozilla-firefox 0.9 in ubuntu hoary RELEASE
    (for ...)
    Started ... ago.
    Buildlog
    Dummy sampledata entry, not processing

When accessed by logged in users, the builder page renders the
'current-status' section with a timestamp at the bottom on the user
timezone. This way they can easily find out if they are reading
outdated information.

    >>> user_browser.open(anon_browser.url)
    >>> print(
    ...     extract_text(
    ...         find_portlet(
    ...             user_browser.contents, "View full history Current status"
    ...         )
    ...     )
    ... )
    View full history Current status
    Building i386 build of mozilla-firefox 0.9 in ubuntu hoary RELEASE
    (for ...)
    Started ... ago.
    Buildlog
    Dummy sampledata entry, not processing
    Updated on ...

The anonymous user can see the builder details portlet and it contains
information about the builder itself, like name, architecture and
location.

    >>> print(
    ...     extract_text(
    ...         find_portlet(anon_browser.contents, "Builder information")
    ...     )
    ... )
    Builder information
    Architectures: 386
    Location: http://localhost:8221/
    Owner: Launchpad Buildd Admins
    Mode: This builder is in auto-mode and accepting jobs from the
    auto-build system.
    Version: 100

The builder status is now displayed as normal text and not as as a
notification alert.

    >>> print_feedback_messages(anon_browser.contents)

Idle builders show more details of their status.

    >>> from lp.buildmaster.enums import BuilderCleanStatus
    >>> from lp.testing import admin_logged_in
    >>> with admin_logged_in():
    ...     builder = factory.makeBuilder(name="victim")
    ...     builder.setCleanStatus(BuilderCleanStatus.DIRTY)
    ...
    >>> anon_browser.open("http://launchpad.test/builders/victim")
    >>> print(
    ...     extract_text(
    ...         find_portlet(
    ...             anon_browser.contents, "View full history Current status"
    ...         )
    ...     )
    ... )
    View full history Current status
    Cleaning
    >>> with admin_logged_in():
    ...     builder.setCleanStatus(BuilderCleanStatus.CLEANING)
    ...
    >>> anon_browser.open(anon_browser.url)
    >>> print(
    ...     extract_text(
    ...         find_portlet(
    ...             anon_browser.contents, "View full history Current status"
    ...         )
    ...     )
    ... )
    View full history Current status
    Cleaning
    >>> with admin_logged_in():
    ...     builder.setCleanStatus(BuilderCleanStatus.CLEAN)
    ...
    >>> anon_browser.open(anon_browser.url)
    >>> print(
    ...     extract_text(
    ...         find_portlet(
    ...             anon_browser.contents, "View full history Current status"
    ...         )
    ...     )
    ... )
    View full history Current status
    Idle


Builder Actions
---------------

All builder actions require authorization, some launchpad.Edit, some
launchpad.Admin, both are only granted to members of
'launchpad-buildd-admins' team and launchpad administrators.

Celso is a member of launchpad-buildd-admins, he has full access to
the builder actions.

    >>> cprov_browser = setupBrowser(
    ...     auth="Basic celso.providelo@canonical.com:test"
    ... )

    >>> cprov_browser.open("http://launchpad.test/builders")
    >>> cprov_browser.getLink("bob").click()

Celso sees both the Change details and Toggle mode actions, which are
displayed in context as shown below. See 'xx-builds-pages.rst' for
documentation about 'Show build history'.'

Celso can modify the location and the visible details of the builder.

    >>> cprov_browser.getLink("Change details").click()
    >>> cprov_browser.getControl(name="field.name").value
    'bob'
    >>> cprov_browser.getControl(name="field.title").value
    'Bob The Builder'
    >>> cprov_browser.getControl(name="field.processors").value
    ['386']
    >>> cprov_browser.getControl(name="field.owner").value
    'launchpad-buildd-admins'

Other administration fields are available on the 'Change details' form
as currently administrators are the only users with permission to use the
Edit page:

    >>> bool(cprov_browser.getControl(name="field.manual").value)
    False

    >>> cprov_browser.getControl(name="field.vm_host").value
    ''

    >>> cprov_browser.getControl(name="field.builderok").value
    True

    >>> cprov_browser.getControl(name="field.failnotes").value
    ''

    >>> cprov_browser.getControl(name="field.active").value
    True

Celso changes the builder title and processors, and the change takes
effect immediately.

    # We use backslashreplace because the page title includes smart quotes.
    >>> from lp.services.helpers import backslashreplace
    >>> print(backslashreplace(cprov_browser.title))
    Change details for...

    >>> title = cprov_browser.getControl(name="field.title")
    >>> original_title = title.value
    >>> title.value = "Donkey builder"
    >>> cprov_browser.getControl(name="field.processors").value = [
    ...     "amd64",
    ...     "hppa",
    ... ]
    >>> cprov_browser.getControl("Change").click()

    # Submitting the change details form redirects to the index page
    # for the builder.
    >>> print(backslashreplace(cprov_browser.title))
    Donkey builder : Build Farm

Then restores it once he realises his mistake.

    >>> cprov_browser.getLink("Change details").click()
    >>> cprov_browser.getControl(name="field.title").value = original_title
    >>> cprov_browser.getControl("Change").click()

By looking at 'details' portlet, Celso realises the builder is in
automatic (AUTO) mode.

    >>> details_portlet = find_portlet(
    ...     cprov_browser.contents, "Builder information"
    ... )
    >>> print(str(extract_text(details_portlet)))
    Builder information
    Architectures: amd64 hppa
    Location: http://localhost:8221/
    Owner: Launchpad Buildd Admins
    Mode: This builder is in auto-mode and accepting jobs from the
    auto-build system. Switch to manual-mode
    Version: 100

He clicks on the Toggle mode button to put the builder into manual mode.

    >>> cprov_browser.getControl(name="field.actions.update").click()

He can see now, in the details portlet that the builder is in manual-mode.

    >>> details_portlet = find_portlet(
    ...     cprov_browser.contents, "Builder information"
    ... )
    >>> print(str(extract_text(details_portlet)))
    Builder information
    ...
    Mode: This builder is in manual-mode and not accepting jobs from the
    auto-build system. Switch to auto-mode
    ...

And a relevant notification is displayed after the mode toggle.

    >>> print_feedback_messages(cprov_browser.contents)
    The builder "Bob The Builder" was updated successfully.

Via the 'edit' form Celso can also modify the 'builderok',
'failure_notes', 'virtualized' and 'virtual machine' fields. All the
consequences of those modifications are covered in the buildd-admin
documentation.

The simplest and most effective thing a administrator can do in this
case is to bring the builder back to work.

    >>> cprov_browser.getLink("Change details").click()
    >>> cprov_browser.getControl(name="field.builderok").value = True
    >>> cprov_browser.getControl(name="field.manual").value = False
    >>> cprov_browser.getControl("Change").click()

Changing the details via the Change details page also generates a
notification.

    >>> print_feedback_messages(cprov_browser.contents)
    The builder "Bob The Builder" was updated successfully.


Marking a builder as inactive
-----------------------------

The builder administrators can hide a builder from the public list
when they judge it convenient, for instance, when the builder present
transient failures or is used for another purpose.

    >>> cprov_browser.open("http://launchpad.test/builders")
    >>> cprov_browser.getLink("bob").click()
    >>> print(backslashreplace(cprov_browser.title))
    Bob The Builder : Build Farm

Celso can toggle the active bit using the Change details form.

    >>> cprov_browser.getLink("Change details").click()
    >>> active = cprov_browser.getControl(name="field.active")
    >>> active.value
    True

    >>> active.value = False
    >>> cprov_browser.getControl("Change").click()

Once deactivated the builder is not be presented in the public Build
Farm list. Celso cannot see the link to it.

    >>> cprov_browser.getLink("Build Farm").click()

    >>> print(extract_text(find_main_content(cprov_browser.contents)))
    The Launchpad build farm
    Register a new build machine
    2 registered build machines: 1 available (0 building, 0 cleaning,
    1 idle) and 1 disabled
    386
    frog   Disabled
    victim Idle
    Updated on ...
    Virtual build status
    Architecture Builders Queue
    386          1        empty

    >>> cprov_browser.getLink("bob").click()
    Traceback (most recent call last):
    ...
    zope.testbrowser.browser.LinkNotFoundError

But Celso can access the deactivated builder via its URL.

    >>> cprov_browser.open("http://launchpad.test/+builds/bob")
    >>> print(backslashreplace(cprov_browser.title))
    Bob The Builder : Build Farm


Actions permissions
-------------------

Normal users, such as No Privileges Person are not shown the
Change details link.

    >>> user_browser.open("http://localhost/+builds/bob")
    >>> user_browser.getLink("Change details")
    Traceback (most recent call last):
    zope.testbrowser.browser.LinkNotFoundError

Nor is the toggle mode control included on the index page.

    >>> user_browser.getControl(name="field.actions.update")
    Traceback (most recent call last):
    LookupError: name ...'field.actions.update' ...

Nor can they access the edit page directly via URL.

    >>> user_browser.open("http://localhost/+builds/bob/+edit")
    Traceback (most recent call last):
    ...
    zope.security.interfaces.Unauthorized: ...

The same is true for the anonymous user:

    >>> anon_browser.open("http://localhost/+builds/bob")
    >>> anon_browser.getLink("Change details")
    Traceback (most recent call last):
    zope.testbrowser.browser.LinkNotFoundError

    >>> anon_browser.getControl(name="field.actions.update")
    Traceback (most recent call last):
    LookupError: name ...'field.actions.update'
    ...

    >>> anon_browser.open("http://localhost/+builds/bob/+edit")
    Traceback (most recent call last):
    ...
    zope.security.interfaces.Unauthorized: ...


Disable a cleaning builder and verify statistics
--------------------------------------------------

First, re-enable bob and set it to cleaning mode. We need to access it via
URL since it was deactivated in the previous section.

    >>> cprov_browser.open("http://launchpad.test/+builds/bob")
    >>> cprov_browser.getLink("Change details").click()
    >>> cprov_browser.getControl(name="field.active").value = True
    >>> cprov_browser.getControl("Change").click()

    >>> from lp.buildmaster.enums import BuilderCleanStatus
    >>> from lp.testing import admin_logged_in
    >>> with admin_logged_in():
    ...     bob_builder = getUtility(IBuilderSet)["bob"]
    ...     bob_builder.builderok = True
    ...     if bob_builder.currentjob is not None:
    ...         bob_builder.currentjob.reset()
    ...     bob_builder.setCleanStatus(BuilderCleanStatus.CLEANING)
    ...

Now verify that the builder appears in the build farm list as cleaning.

    >>> cprov_browser.open("http://launchpad.test/builders")
    >>> print(extract_text(find_main_content(cprov_browser.contents)))
    The Launchpad build farm
    ...
    3 registered build machines: 2 available (0 building, 1 cleaning,
    1 idle) and 1 disabled
    ...
    bob
    Cleaning
    ...

Now disable the builder while it's in cleaning mode.

    >>> cprov_browser.getLink("bob").click()
    >>> cprov_browser.getLink("Change details").click()
    >>> cprov_browser.getControl(name="field.builderok").value = False
    >>> cprov_browser.getControl("Change").click()

Once disabled, the cleaning builder should not appear in the available list.
The statistics should reflect that there are no cleaning builders available.

    >>> cprov_browser.getLink("Build Farm").click()
    >>> print(extract_text(find_main_content(cprov_browser.contents)))
    The Launchpad build farm
    ...
    3 registered build machines: 1 available (0 building, 0 cleaning,
    1 idle) and 2 disabled
    ...
    bob
    Disabled
    ...

    >>> cprov_browser.getLink("bob").click()
    >>> print(backslashreplace(cprov_browser.title))
    Bob The Builder : Build Farm


Verify dirty builders without jobs are counted as cleaning
-----------------------------------------------------------

A builder in DIRTY status without a current job should be counted as cleaning
and should be displayed in the cleaning statistics.

First, create a new builder and set it to dirty status without a job.

    >>> with admin_logged_in():
    ...     dirty_builder = factory.makeBuilder(name="dirty-test")
    ...     dirty_builder.builderok = True
    ...     dirty_builder.setCleanStatus(BuilderCleanStatus.DIRTY)
    ...

Verify that the dirty builder appears in the cleaning statistics.

    >>> cprov_browser.open("http://launchpad.test/builders")
    >>> print(extract_text(find_main_content(cprov_browser.contents)))
    The Launchpad build farm
    ...
    4 registered build machines: 2 available (0 building, 1 cleaning,
    1 idle) and 2 disabled
    ...


