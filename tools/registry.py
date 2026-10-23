"""
Small, dependency-free client for container registries (Docker Hub, GHCR, lscr.io,
quay.io and any other registry that speaks the Docker Registry HTTP API v2).

Used by the update bot and the store checks to:
  * list an image's tags
  * resolve a tag to the digest of its multi-arch index (what we pin to)
  * list the CPU architectures a tag supports
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

INDEX_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
USER_AGENT = "canopy-umbrel-store/1.0 (+https://github.com/Deepshah0308/canopy-umbrel-store)"

_tokens = {}


class RegistryError(RuntimeError):
    pass


def parse_ref(ref):
    """'linuxserver/sonarr:4.0@sha256:..' -> (registry, repository, tag, digest)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        registry, repo = ref.split("/", 1)
    else:
        registry, repo = "registry-1.docker.io", ref
    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"
    if registry == "registry-1.docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag, digest


def display_name(registry, repo):
    if registry == "registry-1.docker.io":
        return repo[len("library/"):] if repo.startswith("library/") else repo
    return f"{registry}/{repo}"


def _request(url, headers=None, method="GET", retries=4):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    delay = 2
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read()
        except (urllib.error.URLError, TimeoutError) as err:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise RegistryError(f"can't reach {url}: {err}")
    raise RegistryError(f"giving up on {url}")


def _token(registry, repo, challenge):
    key = (registry, repo)
    if key in _tokens:
        return _tokens[key]
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
    realm = params.pop("realm", None)
    if not realm:
        raise RegistryError(f"{registry} wants auth but didn't say how")
    params["scope"] = f"repository:{repo}:pull"
    headers = {}
    # Optional Docker Hub login (a free account's read-only token) for higher rate limits in CI.
    user, token = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN")
    if registry == "registry-1.docker.io" and user and token:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
    status, _, body = _request(realm + "?" + urllib.parse.urlencode(params), headers)
    if status != 200:
        raise RegistryError(f"couldn't get a pull token for {repo} ({status})")
    data = json.loads(body)
    _tokens[key] = data.get("token") or data.get("access_token")
    return _tokens[key]


def _get(registry, repo, path, accept=None, method="GET"):
    url = f"https://{registry}/v2/{repo}/{path}"
    headers = {"Accept": accept} if accept else {}
    if (registry, repo) in _tokens:
        headers["Authorization"] = f"Bearer {_tokens[(registry, repo)]}"
    status, hdrs, body = _request(url, headers, method)
    if status == 401:
        challenge = hdrs.get("www-authenticate", "")
        _tokens.pop((registry, repo), None)
        headers["Authorization"] = f"Bearer {_token(registry, repo, challenge)}"
        status, hdrs, body = _request(url, headers, method)
    return status, hdrs, body


def list_tags(ref):
    registry, repo, _, _ = parse_ref(ref)
    tags, path = [], "tags/list?n=1000"
    for _ in range(50):
        status, hdrs, body = _get(registry, repo, path)
        if status != 200:
            raise RegistryError(f"couldn't list tags of {display_name(registry, repo)} ({status})")
        tags += json.loads(body).get("tags") or []
        link = hdrs.get("link")
        match = re.search(r"<([^>]+)>", link or "")
        if not match:
            break
        nxt = match.group(1)
        path = nxt.split(f"/v2/{repo}/", 1)[1] if f"/v2/{repo}/" in nxt else None
        if not path:
            break
    return tags


def manifest(ref, tag=None):
    """(digest, parsed manifest) for a tag, preferring the multi-arch index."""
    registry, repo, ref_tag, _ = parse_ref(ref)
    status, hdrs, body = _get(registry, repo, f"manifests/{tag or ref_tag}", accept=INDEX_TYPES)
    if status != 200:
        raise RegistryError(f"{display_name(registry, repo)}:{tag or ref_tag} not found ({status})")
    digest = hdrs.get("docker-content-digest")
    return digest, json.loads(body)


def _hub_tag(repo, tag):
    """Docker Hub's website API: tag details without touching the registry's pull limit."""
    status, _, body = _request(f"https://hub.docker.com/v2/repositories/{repo}/tags/{urllib.parse.quote(tag)}")
    if status != 200:
        raise RegistryError(f"{repo}:{tag} not found on Docker Hub ({status})")
    return json.loads(body)


def architectures(ref, tag=None):
    """Set of 'os/arch' strings the tag runs on."""
    registry, repo, ref_tag, _ = parse_ref(ref)
    try:
        _, man = manifest(ref, tag)
    except RegistryError as err:
        if registry != "registry-1.docker.io" or "(429)" not in str(err):
            raise
        info = _hub_tag(repo, tag or ref_tag)
        return {f"{i.get('os')}/{i.get('architecture')}" for i in info.get("images") or []
                if i.get("architecture") not in (None, "unknown")}
    if "manifests" in man:
        return {f"{m['platform'].get('os')}/{m['platform'].get('architecture')}"
                for m in man["manifests"]
                if m.get("platform") and m["platform"].get("architecture") not in (None, "unknown")}
    # Single-arch image: read its config blob.
    registry, repo, _, _ = parse_ref(ref)
    cfg = man.get("config", {}).get("digest")
    if not cfg:
        return set()
    status, _, body = _get(registry, repo, f"blobs/{cfg}")
    if status != 200:
        return set()
    data = json.loads(body)
    return {f"{data.get('os')}/{data.get('architecture')}"}


def digest(ref, tag=None):
    """Digest of the tag's index. Uses HEAD, which Docker Hub doesn't count against its pull limit."""
    registry, repo, ref_tag, _ = parse_ref(ref)
    status, hdrs, _ = _get(registry, repo, f"manifests/{tag or ref_tag}", accept=INDEX_TYPES, method="HEAD")
    if status == 200 and hdrs.get("docker-content-digest"):
        return hdrs["docker-content-digest"]
    if status == 429 and registry == "registry-1.docker.io":
        return _hub_tag(repo, tag or ref_tag).get("digest")
    return manifest(ref, tag)[0]


def created(ref, tag=None):
    """When the tag was built (ISO timestamp from the image config), or None."""
    registry, repo, _, _ = parse_ref(ref)
    try:
        _, man = manifest(ref, tag)
        if "manifests" in man:
            pick = next((m for m in man["manifests"] if (m.get("platform") or {}).get("architecture") == "amd64"),
                        None) or man["manifests"][0]
            status, _, body = _get(registry, repo, f"manifests/{pick['digest']}", accept=INDEX_TYPES)
            if status != 200:
                return None
            man = json.loads(body)
        cfg = (man.get("config") or {}).get("digest")
        if not cfg:
            return None
        status, _, body = _get(registry, repo, f"blobs/{cfg}")
        return json.loads(body).get("created") if status == 200 else None
    except (RegistryError, ValueError, KeyError):
        return None
