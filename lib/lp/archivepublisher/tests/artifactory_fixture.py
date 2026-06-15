# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""A fixture that simulates parts of the Artifactory API."""

__all__ = [
    "FakeArtifactoryFixture",
]

import fnmatch
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse

import responses
from fixtures import Fixture


class FakeArtifactoryFixture(Fixture):
    def __init__(self, base_url, repository_name):
        self.base_url = base_url
        self.repository_name = repository_name
        self.repo_url = "%s/%s" % (base_url, self.repository_name)
        self.api_url = "%s/api/storage/%s" % (
            self.base_url,
            self.repository_name,
        )
        self.search_url = "%s/api/search/aql" % self.base_url
        self.metadata_url = "%s/api/metadata/%s" % (
            self.base_url,
            self.repository_name,
        )
        self._fs = {}
        self.add_dir("/")

    def _setUp(self):
        self.requests_mock = responses.RequestsMock(
            assert_all_requests_are_fired=False
        )
        self.requests_mock.start()
        self.addCleanup(self.requests_mock.stop)
        repo_url_regex = re.compile(r"^%s/.*" % re.escape(self.repo_url))
        api_url_regex = re.compile(r"^%s/.*" % re.escape(self.api_url))
        self.requests_mock.add(
            responses.CallbackResponse(
                method="GET",
                url=repo_url_regex,
                callback=self._handle_download,
            )
        )
        self.requests_mock.add_callback(
            "GET", api_url_regex, callback=self._handle_stat
        )
        self.requests_mock.add_callback(
            "PUT", repo_url_regex, callback=self._handle_upload
        )
        self.requests_mock.add_callback(
            "PUT", api_url_regex, callback=self._handle_set_properties
        )
        metadata_url_regex = re.compile(
            r"^%s/.*" % re.escape(self.metadata_url)
        )
        self.requests_mock.add_callback(
            "PATCH", metadata_url_regex, callback=self._handle_update_properties
        )
        self.requests_mock.add_callback(
            "POST", self.search_url, callback=self._handle_aql
        )
        self.requests_mock.add_callback(
            "DELETE", repo_url_regex, callback=self._handle_delete
        )
        self.requests_mock.add_callback(
            "DELETE", api_url_regex, callback=self._handle_delete_properties
        )

    def add_dir(self, path):
        now = datetime.now(timezone.utc).isoformat()
        self._fs[path] = {"created": now, "lastModified": now, "lastUpdated": now}

    def add_file(self, path, contents, size, properties):
        now = datetime.now(timezone.utc).isoformat()
        body = contents.read(size)
        self._fs[path] = {
            "created": now,
            "lastModified": now,
            "lastUpdated": now,
            "size": str(size),
            "checksums": {"sha1": hashlib.sha1(body).hexdigest()},
            "body": body,
            "properties": properties,
        }

    def remove_file(self, path):
        del self._fs[path]

    def _handle_download(self, request):
        """Handle a request to download an existing file."""
        path = unquote(urlparse(request.url[len(self.repo_url) :]).path)
        if path in self._fs and "size" in self._fs[path]:
            return (
                200,
                {"Content-Type": "application/octet-stream"},
                self._fs[path]["body"],
            )
        else:
            return 404, {}, "Unable to find item"

    def _handle_stat(self, request):
        """Handle a request to stat an existing file."""
        parsed_url = urlparse(request.url[len(self.api_url) :])
        path = unquote(parsed_url.path)
        if path in self._fs:
            stat = {"repo": self.repository_name, "path": path}
            stat.update(self._fs[path])
            stat.pop("body", None)
            if parsed_url.query != "properties":
                stat.pop("properties", None)
            return 200, {}, json.dumps(stat)
        else:
            return 404, {}, "Unable to find item"

    def _split(self, delimiter, text):
        """Split text on a delimiter the way that Artifactory does.

        We need to skip delimiters quoted by a leading backslash.
        """
        assert delimiter in ",|=;"
        return re.findall(r"((?:\\[,|=;]|[^%s])+)" % delimiter, text)

    def _unquote(self, text):
        """Unquote something the way that Artifactory does.

        This is poorly-documented.
        https://www.jfrog.com/confluence/display/JFROG/Artifactory+REST+API
        says "In order to supply special characters (comma (,), backslash
        (\\), pipe (|), equals (=)) as key/value you must add an encoded
        backslash (%5C) before them"; but in practice semicolon also needs
        to be quoted in this way to avoid being confused with a property
        separator, and quoting a backslash like this simply results in a
        double backslash.
        """
        return re.sub(r"\\([,|=;])", r"\1", unquote(text))

    def _decode_matrix_parameters(self, encoded):
        """Decode matrix parameters that were encoded as part of a request.

        `ArtifactoryPath.deploy` encodes properties like this.
        """
        properties = defaultdict(list)
        for param in encoded.split(";"):
            key, value = param.split("=", 1)
            properties[unquote_plus(key)].append(unquote_plus(value))
        return properties

    def _decode_properties(self, encoded):
        """Decode properties that were encoded as part of a request.

        `ArtifactoryPath.set_properties` encodes properties like this.
        """
        properties = {}
        for param in self._split(";", encoded):
            key, value = re.match(r"((?:\\[,|=;]|[^=])+)=(.*)", param).groups()
            properties[self._unquote(key)] = [
                self._unquote(v) for v in self._split(",", value)
            ]
        return properties

    def _handle_upload(self, request):
        """Handle a request to upload a directory or file."""
        # Artifactory and urlparse seem to disagree about how to parse URLs
        # where parameters contain the "/" character, so split the
        # parameters from the rest of the URL by hand.
        url = request.url[len(self.repo_url) :]
        if ";" in url:
            url, params = url.split(";", 1)
        else:
            params = ""
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        if path.endswith("/"):
            self.add_dir(path.rstrip("/"))
        elif path.rsplit("/", 1)[0] in self._fs:
            properties = self._decode_matrix_parameters(params)
            self.add_file(
                path,
                request.body,
                int(request.headers["Content-Length"]),
                properties,
            )
        return 201, {}, ""

    def _handle_set_properties(self, request):
        """Handle a request to set properties on an existing file."""
        parsed_url = urlparse(request.url[len(self.api_url) :])
        path = unquote(parsed_url.path)
        if path in self._fs:
            query = parse_qs(parsed_url.query)
            properties = self._decode_properties(query["properties"][0])
            self._fs[path]["properties"].update(properties)
            return 204, {}, ""
        else:
            return 404, {}, "Unable to find item"

    def _handle_update_properties(self, request):
        """Handle a PATCH request to update properties on an existing file.

        In dohq-artifactory >= 1.0, set_properties and del_properties both
        delegate to update_properties, which uses PATCH /api/metadata/{path}
        with a JSON body of {"props": {key: [values] or null}}. A null value
        means delete the property.
        """
        parsed_url = urlparse(request.url[len(self.metadata_url) :])
        path = unquote(parsed_url.path)
        if path not in self._fs:
            return 404, {}, "Unable to find item"
        body = json.loads(request.body)
        props = body.get("props", {})
        for key, value in props.items():
            if value is None:
                self._fs[path]["properties"].pop(key, None)
            else:
                self._fs[path]["properties"][key] = (
                    value if isinstance(value, list) else [value]
                )
        return 204, {}, ""

    def _handle_delete_properties(self, request):
        """Handle a request to delete properties from an existing file."""
        parsed_url = urlparse(request.url[len(self.api_url) :])
        path = unquote(parsed_url.path)
        if path in self._fs:
            query = parse_qs(parsed_url.query)
            for key in query["properties"][0].split(","):
                del self._fs[path]["properties"][unquote(key)]
            return 204, {}, ""
        else:
            return 404, {}, "Unable to find item"

    def _make_aql_item(self, path):
        """Return an AQL response item based on an entry in `self._fs`."""
        path_obj = Path(path)
        item = {
            "repo": self.repository_name,
            "path": path_obj.parent.as_posix()[1:],
            "name": path_obj.name,
            "properties": [],
        }
        for key, value in sorted(self._fs[path]["properties"].items()):
            if value:
                for v in value:
                    item["properties"].append({"key": key, "value": v})
            else:
                item["properties"].append({"key": key})
        return item

    def _matches_aql(self, item, criteria):
        """Return True if an item matches some AQL criteria.

        This is definitely incomplete, but good enough for our testing
        needs.

        https://www.jfrog.com/confluence/display/JFROG/\
          Artifactory+Query+Language
        """
        for key, value in criteria.items():
            if key == "$and":
                if not all(self._matches_aql(item, v) for v in value):
                    return False
            elif key == "$or":
                if not any(self._matches_aql(item, v) for v in value):
                    return False
            elif key.startswith("$"):
                raise ValueError("Unhandled AQL operator: %s" % key)
            elif key in item:
                if isinstance(value, dict) and len(value) == 1:
                    [(comp_op, comp_value)] = value.items()
                    if comp_op == "$match":
                        if not fnmatch.fnmatch(item[key], comp_value):
                            return False
                    else:
                        raise ValueError(
                            "Unhandled AQL comparison operator: %s" % key
                        )
                elif isinstance(value, str):
                    if item[key] != value:
                        return False
                else:
                    raise ValueError("Unhandled AQL criterion: %r" % value)
            else:
                raise ValueError("Unhandled AQL key: %s" % key)
        return True

    def _handle_aql(self, request):
        """Handle a request to perform an AQL search.

        No, of course we don't implement a full AQL parser.
        """
        match = re.match(
            r"^items\.find\((.*?)\)\.include\((.*?)\)$", request.body
        )
        if match is None:
            return 400, {}, ""
        # Treating this as JSON is cheating a bit, but it works.
        criteria = json.loads(match.group(1))

        pattern = r"\.offset\((\d+)\)\.limit\((\d+)\)"

        offset = 0
        limit = 1000
        match = re.search(pattern, request.body)

        if match:
            offset = int(match.group(1))  # Group 1 is the offset value
            limit = int(match.group(2))

        items = [
            self._make_aql_item(path)
            for path in sorted(self._fs)
            if "size" in self._fs[path]
        ]
        results = [item for item in items if self._matches_aql(item, criteria)]
        limit = min(limit, len(results) - offset)
        results = results[offset : offset + limit]
        return 200, {}, json.dumps({"results": results})

    def _handle_delete(self, request):
        """Handle a request to delete an existing file."""
        path = unquote(urlparse(request.url[len(self.repo_url) :]).path)
        if not path.endswith("/") and path in self._fs:
            self.remove_file(path)
        return 200, {}, ""
