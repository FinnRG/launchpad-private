Feeds components
================

`feeds` is a ZCML directive for handling feeds requests.  The
directive requires minimal ZCML configuration and relies on
implementing view classes defining the `usedfor` and `name` class
variables.

Feeds are defined on the `FeedsLayer` layer.


Demonstrating a feeds class
---------------------------

In order to demonstrate a feed class, we need to create an interface
for the thing comprising the feed.  Rather than use a standard
Launchpad interface, we'll define one here.

    >>> from lp.services.feeds.tests.helper import IThing, Thing


ZCML for lp:feeds
-----------------

The zcml `lp:feeds` directive describes a feed view.

    >>> from zope.configuration import xmlconfig
    >>> zcmlcontext = xmlconfig.string(
    ...     """
    ... <configure xmlns:lp="http://namespaces.canonical.com/lp">
    ...   <include package="zope.component" file="meta.zcml" />
    ...   <include package="lp.services.webapp" file="meta.zcml" />
    ...   <lp:feeds
    ...       module="lp.services.feeds.tests.helper"
    ...       classes="ThingFeedView"
    ...       />
    ... </configure>
    ... """
    ... )

Get the view from a `thing` on 'thing-feed' for a request.

    >>> from zope.component import getMultiAdapter
    >>> from lp.services.webapp.servers import LaunchpadTestRequest
    >>> from lp.testing import verifyObject

    >>> request = LaunchpadTestRequest()

To successfully get a view, the lookup must be on an object that is an
`IThing` and the view name must be one that is supported.  If the
view is not found a ComponentLookupError is raised.  Also, the request
must be in the FeedsLayer.

The request we just created is not in the FeedsLayer, so the view will
not be found.

    >>> thing = Thing("thing 1")
    >>> verifyObject(IThing, thing)
    True
    >>> feed_view = getMultiAdapter((thing, request), name="thing-feed.xml")
    Traceback (most recent call last):
      ...
    zope.interface.interfaces.ComponentLookupError: ...

Set the layer on the request for all subsequent uses.

    >>> from lp.layers import setFirstLayer, FeedsLayer
    >>> setFirstLayer(request, FeedsLayer)

If the context object is not an IThing then the view will not be
found.

    >>> thing = object()
    >>> verifyObject(IThing, thing)
    Traceback (most recent call last):
      ...
    zope.interface.exceptions.MultipleInvalid: ...
    Does not declaratively implement the interface
    The lp.services.feeds.tests.helper.IThing.value attribute was not provided
    >>> feed_view = getMultiAdapter((thing, request), name="thing-feed.atom")
    Traceback (most recent call last):
      ...
    zope.interface.interfaces.ComponentLookupError: ...

If the name is not one of the supported names the view will not be
found.

    >>> thing = Thing("thing 1")
    >>> verifyObject(IThing, thing)
    True
    >>> feed_view = getMultiAdapter((thing, request), name="thing-feed.xml")
    Traceback (most recent call last):
      ...
    zope.interface.interfaces.ComponentLookupError: ...

If the thing is an IThing and the name is supported the view will be
found, indicated by the absence of a ComponentLookupError.

    >>> thing = Thing("thing 1")
    >>> verifyObject(IThing, thing)
    True
    >>> for name in ["thing-feed.atom", "thing-feed.html"]:
    ...     feed_view = getMultiAdapter((thing, request), name=name)
    ...     print(feed_view())
    ...
    a feed view on an IThing
    a feed view on an IThing

Feeds Configuration Options
---------------------------

The max_feed_cache_minutes and the max_bug_feed_cache_minutes
configurations are provided to allow overriding the Expires
and Cache-Control headers so that the feeds will be cached longer
by browsers and the reverse-proxy in front of the feeds servers.

    >>> from lp.services.config import config
    >>> from lp.services.feeds.feed import FeedBase
    >>> from lp.bugs.feed.bug import BugsFeedBase
    >>> config.launchpad.max_feed_cache_minutes
    60
    >>> config.launchpad.max_bug_feed_cache_minutes
    30
    >>> FeedBase.max_age
    3600
    >>> BugsFeedBase.max_age
    1800
    >>> config.launchpad.max_feed_cache_minutes * 60 == FeedBase.max_age
    True
    >>> (
    ...     config.launchpad.max_bug_feed_cache_minutes * 60
    ...     == BugsFeedBase.max_age
    ... )
    True


FeedTypedData class
-------------------

The FeedTypedData class performs the appropriate escaping for data
with a type of "text", "html", or "xhtml". The template is responsible
for setting the "type" attribute in the <title> or <content> element. If
no content type is specified, then "text" is assumed.

Since plain text and html are not valid xml, "<", ">", and "&" must
be escaped using xml entities such as "&lt;".

    >>> from lp.services.feeds.feed import FeedTypedData
    >>> text = FeedTypedData("<b> and &nbsp; and &amp;")
    >>> print(text.content)
    &lt;b&gt; and &amp;nbsp; and &amp;amp;
    >>> text2 = FeedTypedData("<b> and &nbsp; and &amp;", content_type="text")
    >>> print(text2.content)
    &lt;b&gt; and &amp;nbsp; and &amp;amp;
    >>> html = FeedTypedData("<b> and &nbsp; and &amp;", content_type="html")
    >>> print(html.content)
    &lt;b&gt; and &amp;nbsp; and &amp;amp;

Since xhtml is valid xml, the "<" and ">" characters do not need to be
escaped. However, xhtml supports many more entities than xml, and Internet
Explorer 7 does not allow a DTD to be specified in feeds which prevents the
xhtml entities from easily being registered in the xml document. Therefore,
the xhtml entities will be converted to valid UTF-8. Since some simplistic
feed readers may expect ascii, we prefer using "html" over "xhtml", however,
we are testing xhtml encoding here in case we need it in the future.

    >>> xhtml = FeedTypedData(
    ...     "<b> and &nbsp; and &amp;</b><hr/>", content_type="xhtml"
    ... )
    >>> print(backslashreplace(xhtml.content))
    <b> and \xa0 and &amp;</b><hr/>

html_page_content vs. content
..............................

`content` is escaped, ready to drop into the Atom XML feed. But an
HTML page only decodes its content once, so embedding `content`
there would leave it double-escaped (e.g. "&amp;" shown as text
instead of "&"). `html_page_content` is the unescaped version for
that case. "text" and "xhtml" content are never escaped twice like
this, so for those it's the same as `content`.

    >>> print(text.html_page_content == text.content)
    True
    >>> print(html.html_page_content)
    <b> and &nbsp; and &amp;
    >>> print(xhtml.html_page_content == xhtml.content)
    True
