DistroSeries
============

From the DerivationOverview spec
<https://launchpad.canonical.com/DerivationOverview>:

    A distribution of GNU/Linux comprises a set of packages, an
    installer, possibly a live-CD, some amount of metadata associated
    with the arrangement of those elements and also a lot of information
    on managing it.

A distro series is a given version of a distribution. So, for Ubuntu, there
are releases (or planned releases) like "warty", "hoary" and "bendy".

Distro releases are retrieved with the IDistroSeriesSet utility, much like
people are retrieved with the IPersonSet utility, or bug tasks are retrieved
with the IBugTaskSet utility.

The IDistroSeriesSet utility is accessed in the usual fashion:


    >>> from zope.component import getUtility
    >>> from lp.testing import verifyObject
    >>> from lp.registry.interfaces.distribution import IDistributionSet
    >>> from lp.registry.interfaces.distroseries import (
    ...     IDistroSeries,
    ...     IDistroSeriesSet,
    ... )
    >>> from lp.translations.interfaces.hastranslationimports import (
    ...     IHasTranslationImports,
    ... )
    >>> distroseriesset = getUtility(IDistroSeriesSet)

To retrieve a specific release of a distribution, use IDistroSeriesSet.get:

    >>> warty = distroseriesset.get(1)
    >>> print(warty.name)
    warty
    >>> print(warty.fullseriesname)
    Ubuntu Warty

To get one specific release by name, use queryByName:

    >>> ubuntu = getUtility(IDistributionSet).getByName("ubuntu")

    >>> warty = distroseriesset.queryByName(ubuntu, "warty")
    >>> print(warty.name)
    warty
    >>> print(distroseriesset.queryByName(ubuntu, "foobar"))
    None

Or IDistroSeriesSet.queryByVersion:

    >>> print(distroseriesset.queryByVersion(ubuntu, "5.04").name)
    hoary
    >>> print(distroseriesset.queryByVersion(ubuntu, "5.05"))
    None

queryByName works on series aliases too if follow_aliases is True.

    >>> ignored = login_person(ubuntu.owner.activemembers[0])
    >>> ubuntu.development_series_alias = "devel"
    >>> login(ANONYMOUS)
    >>> print(distroseriesset.queryByName(ubuntu, "devel"))
    None
    >>> print(
    ...     distroseriesset.queryByName(
    ...         ubuntu, "devel", follow_aliases=True
    ...     ).name
    ... )
    hoary

We verify that a distroseries does in fact fully provide IDistroSeries:

    >>> verifyObject(IDistroSeries, warty)
    True
    >>> IDistroSeries.providedBy(warty)
    True

And IHasTranslationImports:

    >>> verifyObject(IHasTranslationImports, warty)
    True
    >>> IHasTranslationImports.providedBy(warty)
    True

To search the set of IDistroSeriess, use IDistroSeriesSet.search:

    >>> from storm.expr import Desc
    >>> ubuntu_releases = distroseriesset.search(
    ...     distribution=ubuntu, isreleased=True, orderBy=Desc("datereleased")
    ... )
    >>> for release in ubuntu_releases:
    ...     print(release.name)
    ...
    warty

    >>> all_ubuntu_releases = distroseriesset.search(distribution=ubuntu)
    >>> all_ubuntu_releases.count()
    4


Distroseries identifying attributes
-----------------------------------

A distroseries has a set of attributes that identify it. The launchpad id is
the name attribute.

    >>> print(warty.name)
    warty

It has a title for heading and titles...

    >>> print(warty.title)
    The Warty Warthog Release

And a display_name for referring to it in a sentence.

    >>> print(warty.display_name)
    Warty

The fullseriesname attribute is used when the context of the series name
can be confused. Note that the value is created from the launchpad id names
of the distribution and the series, though it may look like the display_name
attributes were used.

    >>> print(warty.fullseriesname)
    Ubuntu Warty

The version attribute holds the debversion of the series.

    >>> print(warty.version)
    4.10

The named_version attribute is used is used to present the series display_name
and version values consistently.

    >>> print(warty.named_version)
    Warty (4.10)


canModifySuite
--------------

canModifySuite method helps us to decide if an upload is allowed or not,
according to the distroseries status and the upload target pocket.

    >>> ubuntu = getUtility(IDistributionSet)["ubuntu"]
    >>> archive = ubuntu.main_archive
    >>> breezy_autotest = ubuntu["breezy-autotest"]
    >>> hoary = ubuntu["hoary"]

    >>> from lp.registry.interfaces.pocket import PackagePublishingPocket
    >>> from lp.registry.interfaces.series import SeriesStatus

    >>> warty.status.name
    'CURRENT'
    >>> archive.canModifySuite(warty, PackagePublishingPocket.RELEASE)
    False
    >>> archive.canModifySuite(warty, PackagePublishingPocket.SECURITY)
    True

    >>> breezy_autotest.status.name
    'EXPERIMENTAL'
    >>> archive.canModifySuite(
    ...     breezy_autotest, PackagePublishingPocket.RELEASE
    ... )
    True
    >>> archive.canModifySuite(
    ...     breezy_autotest, PackagePublishingPocket.SECURITY
    ... )
    False

The FROZEN status is special.  Uploads are allowed for all pockets as
the upload will have to wait for manual approval anyway:

    >>> from zope.security.proxy import removeSecurityProxy
    >>> removeSecurityProxy(hoary).status = SeriesStatus.FROZEN

    >>> hoary.status.name
    'FROZEN'
    >>> archive.canModifySuite(hoary, PackagePublishingPocket.RELEASE)
    True
    >>> archive.canModifySuite(hoary, PackagePublishingPocket.SECURITY)
    True

The PROPOSED pocket is also special.  Pre-release, it may be used for
staging uploads on their way into the RELEASE pocket; post-release, it may
be used for staging uploads on their way into the UPDATES pocket.

    >>> archive.canModifySuite(warty, PackagePublishingPocket.PROPOSED)
    True
    >>> archive.canModifySuite(
    ...     breezy_autotest, PackagePublishingPocket.PROPOSED
    ... )
    True
    >>> archive.canModifySuite(hoary, PackagePublishingPocket.PROPOSED)
    True

Package searching
-----------------

You can search through binary packages publishing in a distribution
release by using the searchPackages method, which uses magical fti:

    >>> warty.searchPackages("pmount").count()
    1

This also works for small or weirdly named packages that don't work
through fti, and even for substrings:

    >>> warty.searchPackages("linux-2.6.12").count()
    1
    >>> warty.searchPackages("at").count()
    1
    >>> pkgs = warty.searchPackages("a")
    >>> for dsbp in pkgs:
    ...     print("%s: %s" % (dsbp.__class__.__name__, dsbp.name))
    ...
    DistroSeriesBinaryPackage: foobar
    DistroSeriesBinaryPackage: mozilla-firefox
    DistroSeriesBinaryPackage: at


DistroSeriess have components and sections
------------------------------------------

A distroseries has some number of components and/or sections which
are valid for that distroseries. These selections are used by (among
other things) the uploader for validating incoming uploads.

    >>> hoary = distroseriesset.get(3)
    >>> for c in hoary.components:
    ...     print(c.name)
    ...
    main
    restricted
    >>> for s in hoary.sections:
    ...     print(s.name)
    ...
    base
    web
    editors
    admin
    devel
    translations

    >>> from lp.soyuz.interfaces.section import ISectionSet
    >>> from lp.soyuz.model.section import SectionSelection
    >>> python = getUtility(ISectionSet).ensure("python")
    >>> _ = SectionSelection(distroseries=hoary, section=python)

    >>> for c in hoary.components:
    ...     print(c.name)
    ...
    main
    restricted

    >>> for s in hoary.sections:
    ...     print(s.name)
    ...
    base
    web
    editors
    admin
    devel
    python
    translations

Breezy-autotest has got a partner component, which is not reported:

    >>> breezyautotest = distroseriesset.queryByName(
    ...     ubuntu, "breezy-autotest"
    ... )
    >>> for c in breezyautotest.components:
    ...     print(c.name)
    ...
    main
    restricted
    universe
    multiverse

The upload_components property, however, reports all the available
components since partner is allowed for upload:

    >>> for c in breezyautotest.upload_components:
    ...     print(c.name)
    ...
    main
    restricted
    universe
    multiverse
    partner


DistroSeries can be initialized from their parents
--------------------------------------------------

When a distroseries is derived from another distroseries (be it a
derivative distribution, or simply the next release in a sequence from
Ubuntu) we need to initialize the new release with quite a lot of
information. Not least of which is the section and component
selections and the publishing information for the distroseries.

DistroSeries provides us with a method for doing this which carefully
goes behind the back of sqlobject to copy potentially tens of
thousands of rows around in order to set up a distroseries.

IDistroSeries lists a series of preconditions for performing an
initialization. In particular the initializer won't overwrite
publishing records etc. Essentially this is a "Do not push this button
again" type set of assertions.

    >>> from lp.soyuz.enums import PackagePublishingStatus
    >>> from lp.soyuz.scripts.initialize_distroseries import (
    ...     InitializeDistroSeries,
    ... )
    >>> login("foo.bar@canonical.com")
    >>> humpy = ubuntu.newSeries(
    ...     "humpy",
    ...     "Humpy Hippo",
    ...     "The Humpy Hippo",
    ...     "Fat",
    ...     "Yo Momma",
    ...     "99.2",
    ...     None,
    ...     hoary.owner,
    ... )
    >>> humpy.previous_series = hoary
    >>> ids = InitializeDistroSeries(humpy, [hoary.id])
    >>> ids.initialize()
    >>> hoary.main_archive.getPublishedSources(
    ...     name="pmount",
    ...     status=PackagePublishingStatus.PUBLISHED,
    ...     distroseries=hoary,
    ...     exact_match=True,
    ... ).count()
    1
    >>> humpy.main_archive.getPublishedSources(
    ...     name="pmount",
    ...     status=PackagePublishingStatus.PUBLISHED,
    ...     distroseries=humpy,
    ...     exact_match=True,
    ... ).count()
    1
    >>> hoary.main_archive.getAllPublishedBinaries(
    ...     distroarchseries=hoary["i386"],
    ...     name="pmount",
    ...     status=PackagePublishingStatus.PUBLISHED,
    ... ).count()
    1
    >>> humpy.main_archive.getAllPublishedBinaries(
    ...     distroarchseries=humpy["i386"], name="pmount"
    ... ).count()
    1

Check if the attributes of an DRSPR instance for the just initialized
distroseries are sane. A DRSPR instance should filter attributes of
a SPR according to the distroseries in question (practically according
what is published in this distrorelease)

Since the InitializeDistroSeries procedure copies the latest
publications from the parent IDRSPR.builds should be empty, reflecting
that there are no builds for this SPR in this DistroSeries.
IDRSPR.builds will be non-empty after a developer submits a new SPR
for the  DistroSeries.

In other hand IDRSPR.binaries should return the binaries resulted of
the SPRs inheritance by joining BPP->BPR->BUILD->SPR, i.e, binaries
published in this distroseries (in fact, in one of its architectures)
resulted of the sourcepackagerelease in question, but built anywhere.
(fix bug #52938)

Initialize a new distroseries based on warty (since it has, at least
one coherent published source + binary, mozilla-firefox)

    >>> bumpy = ubuntu.newSeries(
    ...     "bumpy",
    ...     "Bumpy",
    ...     "The Bumpy",
    ...     "Fat",
    ...     "Boom",
    ...     "99.3",
    ...     None,
    ...     warty.owner,
    ... )
    >>> bumpy.previous_series = warty
    >>> ids = InitializeDistroSeries(bumpy, [warty.id])
    >>> ids.initialize()

Build a new ISourcePackage based in the new distroseries:

    >>> bumpy_firefox_sp = bumpy.getSourcePackage("mozilla-firefox")

Check the content IDSPR binaries & builds attributes:

getBinariesForSeries() should be inherited from parent release.

    >>> bumpy_firefox_sp.currentrelease.getBinariesForSeries(bumpy).count()
    3

    >>> for bin in bumpy_firefox_sp.currentrelease.getBinariesForSeries(
    ...     bumpy
    ... ):
    ...     print(bin.id, bin.title, bin.build.distro_arch_series.title)
    27 mozilla-firefox-data-0.9 The Warty Warthog Release for i386 (386)
    26 mozilla-firefox-0.9 The Warty Warthog Release for hppa (hppa)
    12 mozilla-firefox-0.9 The Warty Warthog Release for i386 (386)

The new series also has the same packaging links as its parent series.

    >>> for packaging in warty.packagings:
    ...     print(packaging.sourcepackagename.name)
    ...
    a52dec
    alsa-utils
    evolution
    mozilla-firefox
    netapplet

    >>> for packaging in bumpy.packagings:
    ...     print(packaging.sourcepackagename.name)
    ...
    a52dec
    alsa-utils
    evolution
    mozilla-firefox
    netapplet


Translatable Packages and Packaging
-----------------------------------

You can easily find out what packages are translatable in a
distribution release:

    >>> translatables = hoary.getTranslatableSourcePackages()
    >>> for translatable in translatables:
    ...     print(translatable.name)
    ...
    evolution
    mozilla
    pmount

Packages can be linked to upstream productseries in specific
distribution releases. IDistroSeries offers a way to query translatable
packages that are linked to upstream productseries.

    >>> from operator import attrgetter
    >>> unlinked_translatables = hoary.getUnlinkedTranslatableSourcePackages()
    >>> for translatable in sorted(
    ...     unlinked_translatables, key=attrgetter("name")
    ... ):
    ...     print(translatable.name)
    mozilla
    pmount

The links to upstream product series can be verified using the
packagings property:

    >>> packagings = hoary.packagings
    >>> for packaging in packagings:
    ...     print(
    ...         packaging.sourcepackagename.name,
    ...         packaging.productseries.product.displayname,
    ...     )
    ...
    evolution Evolution
    mozilla-firefox Mozilla Firefox
    netapplet NetApplet

From the results above you can notice that neither mozilla-firefox nor
netapplet are translatable in Hoary.


Packages that need linking and packagings that need upstream information
-----------------------------------------------------------------------

The distroseries getPrioritizedUnlinkedSourcePackages() method returns
a prioritized list of `ISourcePackage` objects that need a packaging link to
an `IProductSeries` to provide the upstream information to share bugs,
translations, and code. Each item in the list is a dict with the 'package',
total_bugs, and total_messages (translatable messages).

    >>> for summary in hoary.getPrioritizedUnlinkedSourcePackages():
    ...     print(summary["package"].name)
    ...     naked_summary = removeSecurityProxy(summary)
    ...     print("%(bug_count)s %(total_messages)s" % naked_summary)
    ...
    pmount  0  64
    alsa-utils  0  0
    cnews  0  0
    libstdc++  0  0
    linux-source-2.6.15  0  0


The distroseries getPrioritizedPackagings() method that returns a prioritized
list of `IPackaging` that need more information about the upstream project to
share bugs, translations, and code.

    >>> for packaging in hoary.getPrioritizedPackagings():
    ...     print(packaging.sourcepackagename.name)
    ...
    netapplet
    evolution


Most recently linked packagings
-------------------------------

The distroseries getMostRecentlyLinkedPackagings() method returns a
list of up to five packages that are the most recently linked to an
upstream.

    >>> distribution = factory.makeDistribution()
    >>> distroseries = factory.makeDistroSeries(distribution=distribution)
    >>> pkgs = distroseries.getMostRecentlyLinkedPackagings()
    >>> print(pkgs.count())
    0

    >>> for name in ["aaron", "bjorn", "chex", "deryck", "edwin", "francis"]:
    ...     product = factory.makeProduct(name=name)
    ...     productseries = factory.makeProductSeries(product=product)
    ...     spn = factory.makeSourcePackageName(name=name)
    ...     package = factory.makeSourcePackage(
    ...         sourcepackagename=spn, distroseries=distroseries
    ...     )
    ...     package.setPackaging(productseries, product.owner)
    ...     transaction.commit()
    ...


    >>> pkgs = distroseries.getMostRecentlyLinkedPackagings()
    >>> for packaging in pkgs:
    ...     print(packaging.sourcepackagename.name)
    ...
    francis
    edwin
    deryck
    chex
    bjorn


SourcePackagePublishingHistory
------------------------------

ISPP.getPublishedBinaries returns all the binaries generated by the
publication in question:

    >>> warty_pub_source = warty.main_archive.getPublishedSources(
    ...     distroseries=warty,
    ...     name="mozilla-firefox",
    ...     status=PackagePublishingStatus.PUBLISHED,
    ... ).one()
    >>> print(warty_pub_source.sourcepackagerelease.name)
    mozilla-firefox
    >>> print(warty_pub_source.sourcepackagerelease.version)
    0.9
    >>> print(warty_pub_source.component.name)
    main
    >>> print(warty_pub_source.section.name)
    web

    >>> warty_mozilla_pub_binaries = warty_pub_source.getPublishedBinaries()
    >>> warty_mozilla_pub_binaries.count()
    4
    >>> warty_mozilla_pub_bin = warty_mozilla_pub_binaries[0]

    >>> from lp.soyuz.interfaces.publishing import (
    ...     IBinaryPackagePublishingHistory,
    ... )
    >>> verifyObject(IBinaryPackagePublishingHistory, warty_mozilla_pub_bin)
    True

    >>> print(warty_mozilla_pub_bin.binarypackagerelease.name)
    mozilla-firefox
    >>> print(warty_mozilla_pub_bin.binarypackagerelease.version)
    0.9
    >>> print(warty_mozilla_pub_bin.component.name)
    main
    >>> print(warty_mozilla_pub_bin.section.name)
    base

getAllPublishedSources will return all publications with status PUBLISHED
and in the main archives for this distroseries:

    >>> sources = warty.getAllPublishedSources()
    >>> for source in sources:
    ...     print(
    ...         source.sourcepackagerelease.sourcepackagename.name,
    ...         source.sourcepackagerelease.version,
    ...     )
    ...
    netapplet 0.99.6-1
    alsa-utils 1.0.8-1ubuntu1
    alsa-utils 1.0.9a-4
    mozilla-firefox 0.9
    cdrkit 1.0
    iceweasel 1.0

Similarly for binary publications:

    >>> binaries = warty.getAllPublishedBinaries()
    >>> for binary in binaries:
    ...     print(
    ...         binary.binarypackagerelease.binarypackagename.name,
    ...         binary.binarypackagerelease.version,
    ...     )
    ...
    mozilla-firefox 0.9
    pmount 0.1-1
    linux-2.6.12 2.6.12.20
    pmount 2:1.9-1
    at 3.14156
    cdrkit 1.0
    mozilla-firefox 1.0
    mozilla-firefox 0.9
    mozilla-firefox-data 0.9
    mozilla-firefox-data 0.9


Creating DistroSeries
---------------------

Users with launchpad.Driver permission may create DistroSeries. In the
case of a distribution that doesn't use Soyuz officially, a user who is
a driver can create the series and they are automatically assigned to the
series' driver role so that they can edit it.

    >>> youbuntu = factory.makeDistribution(name="youbuntu")
    >>> yo_driver = factory.makePerson(name="yo-driver")
    >>> youbuntu.driver = yo_driver
    >>> ignored = login_person(yo_driver)
    >>> youbuntu.official_packages
    False

    >>> yo_series = youbuntu.newSeries(
    ...     name="island",
    ...     display_name="Island",
    ...     title="YouBuntu Island",
    ...     summary="summary",
    ...     description="description",
    ...     version="09.07",
    ...     previous_series=warty,
    ...     registrant=yo_driver,
    ... )
    >>> print(yo_series.name)
    island
    >>> print(yo_series.registrant.name)
    yo-driver
    >>> print(yo_series.driver.name)
    yo-driver

Owners of derivative distributions, and admins can create series too, but
they are not automatically set as the series driver because they always
have permission to edit the series.

    >>> ignored = login_person(youbuntu.owner)
    >>> yo_series = youbuntu.newSeries(
    ...     name="forest",
    ...     display_name="Forest",
    ...     title="YouBuntu Forest",
    ...     summary="summary",
    ...     description="description",
    ...     version="09.07",
    ...     previous_series=warty,
    ...     registrant=youbuntu.owner,
    ... )
    >>> print(yo_series.name)
    forest
    >>> print(yo_series.driver)
    None

Ubuntu uses Launchpad for package managemtn, so it requires special
preparation for Soyuz and Translations before a series can be created.
Ubuntu driver can not create series.

    >>> ignored = login_person(ubuntu.owner.activemembers[0])
    >>> ubuntu.driver = yo_driver
    >>> ignored = login_person(yo_driver)
    >>> ubuntu.newSeries(
    ...     name="finch",
    ...     display_name="Finch",
    ...     title="Ubuntu Finch",
    ...     summary="summary",
    ...     description="description",
    ...     version="9.06",
    ...     previous_series=warty,
    ...     owner=ubuntu.driver,
    ... )
    Traceback (most recent call last):
     ...
    zope.security.interfaces.Unauthorized: ...

Owners and admins of base distributions are the only users who can create a
series.

    >>> ignored = login_person(ubuntu.owner.activemembers[0])
    >>> u_series = ubuntu.newSeries(
    ...     name="finch",
    ...     display_name="Finch",
    ...     title="Ubuntu Finch",
    ...     summary="summary",
    ...     description="description",
    ...     version="9.06",
    ...     previous_series=warty,
    ...     registrant=ubuntu.owner,
    ... )
    >>> print(u_series.name)
    finch
    >>> print(u_series.registrant.name)
    ubuntu-team
    >>> print(u_series.driver)
    None


Specification Listings
----------------------

We should be able to get lists of specifications in different states
related to a distroseries.

Basically, we can filter by completeness, and by whether or not the spec is
informational.

    >>> distroset = getUtility(IDistributionSet)
    >>> kubuntu = distroset.getByName("kubuntu")
    >>> krunch = kubuntu.getSeries("krunch")
    >>> from lp.blueprints.enums import SpecificationFilter

First, there should be one informational specs for krunch:

    >>> filter = [SpecificationFilter.INFORMATIONAL]
    >>> krunch.specifications(None, filter=filter).count()
    1


There are 2 completed specs for Krunch:

    >>> filter = [SpecificationFilter.COMPLETE]
    >>> for spec in kubuntu.specifications(None, filter=filter):
    ...     print(spec.name, spec.is_complete)
    ...
    thinclient-local-devices True
    usplash-on-hibernation True


And there are 2 incomplete specs:

    >>> filter = [SpecificationFilter.INCOMPLETE]
    >>> for spec in krunch.specifications(None, filter=filter):
    ...     print(spec.name, spec.is_complete)
    ...
    cluster-installation False
    revu False


If we ask for all specs, we get them in the order of priority.

    >>> filter = [SpecificationFilter.ALL]
    >>> for spec in krunch.specifications(None, filter=filter):
    ...     print(spec.priority.title, spec.name)
    ...
    Essential cluster-installation
    High revu
    Medium thinclient-local-devices
    Low usplash-on-hibernation
    Undefined kde-desktopfile-langpacks
    Not krunch-desktop-plan


With a distroseries, we can ask for ACCEPTED, PROPOSED and DECLINED specs:

    >>> filter = [SpecificationFilter.ACCEPTED]
    >>> for spec in krunch.specifications(None, filter=filter):
    ...     print(spec.name, spec.goalstatus.title)
    ...
    cluster-installation Accepted
    revu Accepted
    thinclient-local-devices Accepted
    usplash-on-hibernation Accepted

    >>> filter = [SpecificationFilter.PROPOSED]
    >>> for spec in krunch.specifications(None, filter=filter):
    ...     print(spec.name, spec.goalstatus.title)
    ...
    kde-desktopfile-langpacks Proposed

    >>> filter = [SpecificationFilter.DECLINED]
    >>> for spec in krunch.specifications(None, filter=filter):
    ...     print(spec.name, spec.goalstatus.title)
    ...
    krunch-desktop-plan Declined


And if we ask just for specs, we get BOTH the incomplete and the complete
ones that have been accepted.

    >>> for spec in krunch.specifications(None):
    ...     print(spec.name, spec.is_complete, spec.goalstatus.title)
    ...
    cluster-installation False Accepted
    revu False Accepted
    thinclient-local-devices True Accepted
    usplash-on-hibernation True Accepted

We can filter for specifications that contain specific text:

    >>> for spec in krunch.specifications(None, filter=["usb"]):
    ...     print(spec.name)
    ...
    thinclient-local-devices


Drivers
=======

Distributions have drivers, who are people that have permission to approve
bugs and features for specific releases. The rules are that:

 1. a "driver" can be set on either Distribution or DistroSeries
 2. drivers are only actually relevant on a DistroSeries, because that's the
    granularity at which we track spec/bug targeting
 3. the important attribute is ".drivers" on a distroseries, it is
    calculated based on the combination of owners and drivers in the
    distribution and the distroseries. It is a LIST of drivers, which might
    be empty, or have one or two people/teams in it.
 4. If the release has a driver, then that driver is in the list.
 5. If the distribution has a driver then that is in the list too, otherwise
 6. If neither the release nor the distribution has a driver, then the
    distribution registrant is the driver.

We test these rules below.


First, we look at a release where both the distribution and release have
drivers. Kubuntu should be a good example.

    >>> print(kubuntu.driver.name)
    jblack
    >>> print(krunch.driver.name)
    edgar
    >>> for d in krunch.drivers:
    ...     print(d.name)
    ...
    edgar
    jblack


Now, we look at a release where there is a driver on the release but not on
the distribution.

    >>> debian = distroset.getByName("debian")
    >>> print(debian.driver)
    None
    >>> print(debian.owner.name)
    mark
    >>> sarge = debian.getSeries("sarge")
    >>> print(sarge.driver.name)
    jdub
    >>> for d in sarge.drivers:
    ...     print(d.name)
    ...
    jdub
    mark


Now, a release where there is no driver on the release but there is a driver
on the distribution.

    >>> redhat = distroset.getByName("redhat")
    >>> print(redhat.driver.name)
    jblack
    >>> six = redhat.getSeries("six")
    >>> print(six.driver)
    None
    >>> for d in six.drivers:
    ...     print(d.name)
    ...
    jblack

Finally, on a release where neither the distribution nor the release have a
driver. Here, we expect the driver to be the owner of the distribution
(because this is the "commonest fallback").

    >>> sid = debian.getSeries("sid")
    >>> print(debian.driver)
    None
    >>> print(debian.owner.name)
    mark
    >>> print(sid.driver)
    None
    >>> print(sid.registrant.name)
    jdub

    >>> for d in sid.drivers:
    ...     print(d.name)
    ...
    mark


Latest Uploads
--------------

IDistroSeries provides the 'getLatestUpload' method which returns a
list of the last 5 (five) IDistributionSourcePackageRelease (IDSPR)
uploaded and published in its context.

    >>> warty = ubuntu["warty"]
    >>> latest_uploads = warty.getLatestUploads()

Each element is an IDistributionSourcePackageRelease instance:

    >>> for upload in latest_uploads:
    ...     print(upload.title)
    ...
    mozilla-firefox 0.9 source package in Ubuntu

Also, empty results (caused obviously by lack of sample data or very
earlier development state of a distroseries) are possible:

    >>> ubuntutest = getUtility(IDistributionSet)["ubuntutest"]
    >>> breezy_autotest = ubuntutest["breezy-autotest"]
    >>> latest_uploads = breezy_autotest.getLatestUploads()

    >>> len(latest_uploads)
    0


Getting build records for a distro series
-----------------------------------------

IDistroSeries inherits the IHasBuildRecords interfaces and therefore provides
a getBuildRecords() method.

    >>> builds = ubuntu["warty"].getBuildRecords(name="firefox")
    >>> for build in builds:
    ...     print(build.title)
    ...
    hppa build of mozilla-firefox 0.9 in ubuntu warty RELEASE
    i386 build of mozilla-firefox 0.9 in ubuntu warty RELEASE

For further options that can be used with getBuildRecords(), please
see hasbuildrecords.rst

The ``devel`` web API also provides ``getBuildRecordsCounts`` to efficiently
summarize distro-archive binary package build records by state and pocket.
Filters are optional and apply before counting.  Finish times use
``finished_since <= date_finished < finished_before``, and the bounds must
not be reversed.  Successful build records without a finish time are excluded.

The response always contains ``total``, ``by_build_state``, and ``by_pocket``.
Both mappings contain every value from their enum, keyed by stable enum name
and zero-filled when necessary.  Empty results use the same shape with all
counts set to zero, and each mapping independently sums to ``total``.
