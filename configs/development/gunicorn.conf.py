import os
from fnmatch import fnmatch

BASE_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
CONFIG_DIR = os.path.dirname(__file__)


def find_files(directory, pattern):
    """Find files in `directory` matching `pattern`."""
    result = []
    for root, _, files in os.walk(directory):
        for basename in files:
            matches = fnmatch(basename, pattern)
            if matches:
                filename = os.path.join(root, basename)
                result.append(filename)
    return result


bind = [":8085", ":8086", ":8087", ":8088", ":8089"]
workers = 2
threads = 10
max_requests = 1000
log_level = "DEBUG"

# Keeping a really long timeout in development to allow us to ipdb calmly.
timeout = 15 * 60

reload = True
# Watch config files changes from the source tree.
reload_extra_files = find_files(CONFIG_DIR, "*")
for pattern in ["*.zcml", "*.conf"]:
    reload_extra_files += find_files(os.path.join(BASE_DIR, "lib"), pattern)


def post_fork(server, worker):
    """Re-configure the OpenID fetcher after gunicorn module reloads.

    gunicorn's reload=True uses a module reloader that resets openid.fetchers'
    _default_fetcher global to None, losing the custom SSL context configured
    by lp_sitecustomize.  Calling set_default_openid_fetcher() here runs after
    each worker fork/reload, ensuring the fetcher is always correctly set.
    """
    from lp.services.openid.fetcher import set_default_openid_fetcher

    set_default_openid_fetcher()
