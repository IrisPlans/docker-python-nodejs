import argparse
import json
import os
import re
from datetime import datetime
from functools import cmp_to_key
from io import BytesIO
from jinja2 import Environment, PackageLoader, select_autoescape, FileSystemLoader
from pathlib import Path

import docker
import docker.errors
import requests
import semver

from requests_html import HTMLSession

DOCKER_IMAGE_NAME = "nikolaik/python-nodejs"
VERSIONS_PATH = Path("versions.json")
DEFAULT_DISTRO = "buster"
DISTROS = ["buster", "bullseye", "slim", "alpine"]

todays_date = datetime.utcnow().date().isoformat()


by_semver_key = cmp_to_key(semver.compare)

env = Environment(loader=FileSystemLoader("./templates"), autoescape=select_autoescape())


def _render_template(template_name, **context):
    template = env.get_template(template_name)
    return template.render(**context)


def _fetch_tags(package):
    # Fetch available docker tags
    result = requests.get(f"https://registry.hub.docker.com/v1/repositories/{package}/tags")
    return [r["name"] for r in result.json()]


def _latest_patch(tags, ver, patch_pattern, distro):
    tags = [tag for tag in tags if tag.startswith(ver) and tag.endswith(f"-{distro}") and patch_pattern.match(tag)]
    return sorted(tags, key=by_semver_key, reverse=True)[0] if tags else ""


def _fetch_node_gpg_keys():
    url = "https://raw.githubusercontent.com/nodejs/docker-node/master/keys/node.keys"
    return requests.get(url).text.replace("\n", " ")


def scrape_supported_python_versions():
    """Scrape supported python versions (risky)"""
    versions = []
    version_table_selector = "#status-of-python-branches table"

    r = HTMLSession().get("https://devguide.python.org/")
    version_table = r.html.find(version_table_selector, first=True)
    for ver in version_table.find("tbody tr"):
        branch, _, _, first_release, end_of_life, _ = [v.text for v in ver.find("td")]
        versions.append({"version": branch, "start": first_release, "end": end_of_life})

    return versions


def fetch_supported_nodejs_versions():
    result = requests.get("https://raw.githubusercontent.com/nodejs/Release/master/schedule.json")
    return [{"version": ver, "start": detail["start"], "end": detail["end"]} for ver, detail in result.json().items()]


def decide_python_versions(distros):
    python_patch_re = "|".join([r"^(\d+\.\d+\.\d+-{})$".format(distro) for distro in distros])
    python_wanted_tag_pattern = re.compile(python_patch_re)

    tags = [tag for tag in _fetch_tags("python") if python_wanted_tag_pattern.match(tag)]
    # Skip unreleased and unsupported
    supported_versions = [v for v in scrape_supported_python_versions() if v["start"] <= todays_date <= v["end"]]

    versions = []
    for supported_version in supported_versions:
        ver = supported_version["version"]
        for distro in distros:
            canonical_image = _latest_patch(tags, ver, python_wanted_tag_pattern, distro)
            if not canonical_image:
                print(f"Not good. ver={ver} distro={distro} not in tags, skipping...")
                continue
            canonical_version = canonical_image.replace(f"-{distro}", "")
            versions.append(
                {"canonical_version": canonical_version, "image": canonical_image, "key": ver, "distro": distro}
            )

    return sorted(versions, key=lambda v: by_semver_key(v["canonical_version"]), reverse=True)


def decide_nodejs_versions():
    nodejs_patch_re = r"^(\d+\.\d+\.\d+-{})$".format(DEFAULT_DISTRO)
    nodejs_wanted_tag_pattern = re.compile(nodejs_patch_re)

    tags = [tag for tag in _fetch_tags("node") if nodejs_wanted_tag_pattern.match(tag)]
    # Skip unreleased and unsupported
    supported_versions = [v for v in fetch_supported_nodejs_versions() if v["start"] <= todays_date <= v["end"]]

    versions = []
    for supported_version in supported_versions:
        ver = supported_version["version"][1:]  # Remove v prefix
        canonical_image = _latest_patch(tags, ver, nodejs_wanted_tag_pattern, DEFAULT_DISTRO)
        if not canonical_image:
            print(f"Not good, ver={ver} distro={DEFAULT_DISTRO} not in tags, skipping...")
            continue
        canonical_version = canonical_image.replace(f"-{DEFAULT_DISTRO}", "")
        versions.append({"canonical_version": canonical_version, "key": ver})

    return sorted(versions, key=lambda v: by_semver_key(v["canonical_version"]), reverse=True)


def version_combinations(nodejs_versions, python_versions):
    versions = []
    for p in python_versions:
        for n in nodejs_versions:
            distro = f'-{p["distro"]}' if p["distro"] != DEFAULT_DISTRO else ""
            key = f'python{p["key"]}-nodejs{n["key"]}{distro}'
            versions.append(
                {
                    "key": key,
                    "python": p["key"],
                    "python_canonical": p["canonical_version"],
                    "python_image": p["image"],
                    "nodejs": n["key"],
                    "nodejs_canonical": n["canonical_version"],
                    "distro": p["distro"],
                }
            )

    versions = sorted(versions, key=lambda v: DISTROS.index(v["distro"]))
    versions = sorted(versions, key=lambda v: by_semver_key(v["nodejs_canonical"]), reverse=True)
    versions = sorted(versions, key=lambda v: by_semver_key(v["python_canonical"]), reverse=True)
    return versions


def render_dockerfile(version, node_gpg_keys):
    dockerfile_template = Path(f'templates/{version["distro"]}.Dockerfile').read_text()
    replace_pattern = re.compile("%%(.+?)%%")

    replacements = {
        # NPM: Hold back on v7 for nodejs<15 until `npm install -g npm` installs v7
        "npm_version": "6" if int(version["nodejs"]) < 15 else "7",
        "now": datetime.utcnow().isoformat()[:-7],
        "node_gpg_keys": node_gpg_keys,
        **version,
    }

    def repl(matchobj):
        key = matchobj.group(1).lower()
        return replacements[key]

    return replace_pattern.sub(repl, dockerfile_template)


def persist_versions(versions, dry_run=False):
    if dry_run:
        return
    with VERSIONS_PATH.open("w+") as fp:
        json.dump({"versions": versions}, fp, indent=2)


def load_versions():
    with VERSIONS_PATH.open() as fp:
        return json.load(fp)["versions"]


def build_new_or_updated(current_versions, versions, dry_run=False, debug=False, force=False):
    # Find new or updated
    current_versions = {ver["key"]: ver for ver in current_versions}
    versions = {ver["key"]: ver for ver in versions}
    new_or_updated = []

    for key, ver in versions.items():
        updated = key in current_versions and ver != current_versions[key]
        new = key not in current_versions
        if new or updated or force:
            new_or_updated.append(ver)

    if not new_or_updated:
        print("No new or updated versions")
        return

    # Login to docker hub
    docker_client = docker.from_env()
    dockerhub_username = os.getenv("DOCKERHUB_USERNAME")
    try:
        docker_client.login(dockerhub_username, os.getenv("DOCKERHUB_PASSWORD"))
    except docker.errors.APIError:
        print(f"Could not login to docker hub with username:'{dockerhub_username}'.")
        print("Is env var DOCKERHUB_USERNAME and DOCKERHUB_PASSWORD set correctly?")
        exit(1)

    node_gpg_keys = _fetch_node_gpg_keys()
    # Build, tag and push images
    for version in new_or_updated:
        dockerfile = render_dockerfile(version, node_gpg_keys)
        # docker build wants bytes
        with BytesIO(dockerfile.encode()) as fileobj:
            tag = f"{DOCKER_IMAGE_NAME}:{version['key']}"
            nodejs_version = version["nodejs_canonical"]
            python_version = version["python_canonical"]
            print(
                f"Building image {version['key']} python: {python_version} nodejs: {nodejs_version} ...",
                end="",
                flush=True,
            )
            if not dry_run:
                docker_client.images.build(fileobj=fileobj, tag=tag, rm=True, pull=True)
            if debug:
                with Path(f"debug-{version['key']}.Dockerfile").open("w") as debug_file:
                    debug_file.write(fileobj.read().decode("utf-8"))
            print(f" pushing...", flush=True)
            if not dry_run:
                docker_client.images.push(DOCKER_IMAGE_NAME, version["key"])


def update_readme_tags_table(versions, dry_run=False):
    readme_path = Path("README.md")
    with readme_path.open() as fp:
        readme = fp.read()

    headings = ["Tag", "Python version", "Node.js version", "Distro"]
    rows = []
    for v in versions:
        rows.append([f"`{v['key']}`", v["python_canonical"], v["nodejs_canonical"], v["distro"]])

    head = f"{' | '.join(headings)}\n{' | '.join(['---' for h in headings])}"
    body = "\n".join([" | ".join(row) for row in rows])
    table = f"{head}\n{body}\n"

    start = "the following table of available image tags.\n"
    end = "\nLovely!"
    sub_pattern = re.compile(f"{start}(.+?){end}", re.MULTILINE | re.DOTALL)

    readme_new = sub_pattern.sub(f"{start}\n{table}{end}", readme)
    if readme != readme_new and not dry_run:
        with readme_path.open("w+") as fp:
            fp.write(readme_new)


def main(distros, dry_run, debug, force):
    distros = list(set(distros))
    current_versions = load_versions()
    # Use latest patch version from each minor
    python_versions = decide_python_versions(distros)
    # Use latest minor version from each major
    nodejs_versions = decide_nodejs_versions()
    versions = version_combinations(nodejs_versions, python_versions)

    persist_versions(versions, dry_run)
    update_readme_tags_table(versions, dry_run)

    # Build tag and release docker images
    build_new_or_updated(current_versions, versions, dry_run, debug, force)

    # FIXME(perf): Generate a CircleCI config file with a workflow (parallel) and trigger this workflow via the API.
    # Ref: https://circleci.com/docs/2.0/api-job-trigger/
    # Ref: https://discuss.circleci.com/t/run-builds-on-circleci-using-a-local-config-file/17355?source_topic_id=19287


if __name__ == "__main__":
    parser = argparse.ArgumentParser(usage="🐳 Build Python with Node.js docker images")
    parser.add_argument(
        "-d",
        "--distros",
        dest="distros",
        nargs="*",
        choices=DISTROS,
        help="Specify which distros to build",
        default=DISTROS,
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="Skip persisting, README update, and pushing of builds"
    )
    parser.add_argument("--debug", action="store_true", help="Write generated dockerfiles to disk")
    parser.add_argument("--force", action="store_true", help="Force build all versions (even old)")
    args = vars(parser.parse_args())
    main(**args)
