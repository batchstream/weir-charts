"""Fail closed before publishing an existing GitHub/OCI version.

Uses only the workflow-provided GH_TOKEN; never writes or prints credentials.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def request(url, headers, method="GET"):
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, b""


def main():
    version = sys.argv[1]
    repository = os.environ["GITHUB_REPOSITORY"]
    actor = os.environ["GITHUB_ACTOR"]
    token = os.environ["GH_TOKEN"]
    github_headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    status, _ = request("https://api.github.com/repos/" + repository, github_headers)
    if status != 200:
        raise RuntimeError("repository access check failed: HTTP " + str(status))
    tag = urllib.parse.quote("weir-" + version, safe="")
    status, _ = request("https://api.github.com/repos/" + repository + "/releases/tags/" + tag, github_headers)
    if status != 404:
        raise RuntimeError("release must be absent (HTTP 404); observed HTTP " + str(status))
    auth = base64.b64encode((actor + ":" + token).encode()).decode()
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": "repository:batchstream/charts/weir:pull,push"})
    auth_headers = {"Authorization": "Basic " + auth}
    status, body = request("https://ghcr.io/token?" + query, auth_headers)
    if status != 200:
        raise RuntimeError("registry authorization failed: HTTP " + str(status))
    registry_token = json.loads(body)["token"]
    registry_headers = {
        "Authorization": "Bearer " + registry_token,
        "Accept": "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json",
    }
    tag = urllib.parse.quote(version, safe="")
    status, _ = request("https://ghcr.io/v2/batchstream/charts/weir/manifests/" + tag, registry_headers, "HEAD")
    if status != 404:
        raise RuntimeError("chart version must be absent (HTTP 404); observed HTTP " + str(status))
    print("Verified: GitHub release and authenticated OCI version are absent")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, urllib.error.URLError, TimeoutError, OSError) as error:
        print("Release preflight refused: " + type(error).__name__ + ": " + str(error), file=sys.stderr)
        sys.exit(1)
