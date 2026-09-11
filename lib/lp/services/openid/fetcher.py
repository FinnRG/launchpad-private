# Copyright 2009-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""OpenID consumer configuration."""

__all__ = [
    "set_default_openid_fetcher",
]

import os.path
import ssl
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from openid.fetchers import Urllib2Fetcher, setDefaultFetcher

from lp.services.config import config
from lp.services.encoding import wsgi_native_string


class WSGIFriendlyUrllib2Fetcher(Urllib2Fetcher):
    def fetch(self, url, body=None, headers=None):
        if headers is not None:
            headers = {
                wsgi_native_string(key): wsgi_native_string(value)
                for key, value in headers.items()
            }
        return super().fetch(url, body=body, headers=headers)


def set_default_openid_fetcher():
    # Make sure we're using the same fetcher that we use in production, even
    # if pycurl is installed.
    fetcher = WSGIFriendlyUrllib2Fetcher()

    handlers = []

    if config.launchpad.openid_http_proxy:
        proxy_handler = ProxyHandler(
            {
                "http": config.launchpad.openid_http_proxy,
                "https": config.launchpad.openid_http_proxy,
            }
        )
        handlers.append(proxy_handler)

    if config.launchpad.enable_test_openid_provider:
        # Tests have an instance name that looks like 'testrunner-appserver'
        # or similar. We're in 'development' there, so just use that config.
        if config.instance_name.startswith("testrunner"):
            instance_name = "development"
        else:
            instance_name = config.instance_name
        cert_path = f"configs/{instance_name}/launchpad-ca.crt"
        cafile = os.path.join(config.root, cert_path)

        # Create SSL context with certificate and add HTTPS handler
        if os.path.exists(cafile):
            ssl_context = ssl.create_default_context(cafile=cafile)
            https_handler = HTTPSHandler(context=ssl_context)
            handlers.append(https_handler)

    if handlers:
        opener = build_opener(*handlers)
        fetcher.urlopen = opener.open

    setDefaultFetcher(fetcher)
