import logging
import tempfile
import subprocess as sp
import os
from pathlib import Path
import json
import calendar
import time
import urllib
import tarfile

from jinja2 import Environment
from github import Github
from github.ContentFile import ContentFile
from github.GithubException import UnknownObjectException, RateLimitExceededException
import git
from jinja2 import Environment, FileSystemLoader, select_autoescape
import yaml

logging.basicConfig(level=logging.INFO)

test_repo = os.environ.get("TEST_REPO")

env = Environment(
    autoescape=select_autoescape(["html"]), loader=FileSystemLoader("templates")
)

# do not clone LFS files
os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"
g = Github(os.environ["GITHUB_TOKEN"])
get_rate_limit = lambda api_type: getattr(g.get_rate_limit(), api_type)

with open("data.js", "r") as f:
    next(f)
    previous_repos = {repo["full_name"]: repo for repo in json.loads(f.read())}

with open("skips.json", "r") as f:
    previous_skips = {repo["full_name"]: repo for repo in json.load(f)}

blacklist = set(l.strip() for l in open("blacklist.txt", "r"))

repos = []
skips = []


def register_skip(repo):
    skips.append(
        {"full_name": repo.full_name, "updated_at": repo.updated_at.timestamp()}
    )


class Repo:
    data_format = 2

    def __init__(
        self,
        github_repo,
        linting,
        formatting,
        config_readme,
        settings: dict,
    ):
        for attr in [
            "full_name",
            "description",
            "stargazers_count",
            "subscribers_count",
        ]:
            setattr(self, attr, getattr(github_repo, attr))

        self.topics = github_repo.get_topics()
        self.updated_at = github_repo.updated_at.timestamp()

        self.linting = linting

        self.formatting = formatting
        if formatting is not None:
            self.formatting += f"\nsnakefmt version: {snakefmt_version}"

        try:
            self.latest_release = github_repo.get_latest_release().tag_name
        except UnknownObjectException:
            # no release
            self.latest_release = None

        if settings is not None and config_readme is not None:
            self.mandatory_flags = settings.get("usage", {}).get(
                "mandatory-flags", None
            )
            self.report = settings.get("usage", {}).get("report", False)
            self.software_stack_deployment = settings.get("usage", {}).get(
                "software-stack-deployment", {}
            )
            self.config_readme = g.render_markdown(config_readme)
            self.standardized = True
        else:
            self.mandatory_flags = []
            self.software_stack_deployment = None
            self.config_readme = None
            self.report = False
            self.standardized = False

        # increase this if fields above change
        self.data_format = Repo.data_format


def rate_limit_wait(api_type):
    curr_timestamp = calendar.timegm(time.gmtime())
    reset_timestamp = calendar.timegm(get_rate_limit(api_type).reset.timetuple())
    # add 5 seconds to be sure the rate limit has been reset
    sleep_time = max(0, reset_timestamp - curr_timestamp) + 5
    logging.warning(f"Rate limit exceeded, waiting {sleep_time} seconds")
    time.sleep(sleep_time)


def call_rate_limit_aware(func, api_type="core"):
    while True:
        try:
            return func()
        except RateLimitExceededException:
            rate_limit_wait(api_type)


def call_rate_limit_aware_decorator(func):
    def inner(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except RateLimitExceededException:
                rate_limit_wait("core")

    return inner


def store_data():
    repos.sort(key=lambda repo: repo["stargazers_count"])

    with open("data.js", "w") as out:
        print(env.get_template("data.js").render(data=repos), file=out)
    with open("skips.json", "w") as out:
        json.dump(skips, out, sort_keys=True, indent=2)


@call_rate_limit_aware_decorator
def check_repo_exists(g, full_name):
    try:
        g.get_repo(full_name)
        return True
    except UnknownObjectException:
        logging.info(f"Repo {full_name} has been deleted")
        return False


if test_repo is not None:
    repo_search = [g.get_repo(test_repo)]
    total_count = 1
else:
    repo_search = g.search_repositories(
        "nextflow in:readme archived:false", sort="updated"
    )
    total_count = call_rate_limit_aware(
        lambda: repo_search.totalCount, api_type="search"
    )

logging.info(f"Checking {total_count} repos.")

for i in range(total_count):
    # We access each repo by index instead of using an iterator
    # in order to be able to retry the access in case we reach the search
    # rate limit.
    repo = call_rate_limit_aware(lambda: repo_search[i], api_type="search")

    if i % 10 == 0:
        logging.info(f"{i} of {total_count} repos done.")

    log_skip = lambda reason: logging.info(
        f"Skipped {repo.full_name} because {reason}."
    )

    logging.info(f"Processing {repo.full_name}.")
    if repo.full_name in blacklist:
        log_skip("it is blacklisted")
        continue

    updated_at = repo.updated_at
    try:
        release = call_rate_limit_aware(repo.get_latest_release)
        updated_at = max(updated_at, release.created_at)
    except UnknownObjectException:
        release = None

    prev = previous_repos.get(repo.full_name)
    if (
        prev is not None
        and Repo.data_format == prev["data_format"]
        and prev["updated_at"] == updated_at.timestamp()
    ):
        # keep old data, it hasn't changed
        logging.info("Remaining repos haven't changed, using old data.")
        logging.info("Repo hasn't changed, keeping old data.")
        repos.append(prev)
        continue

    prev_skip = previous_skips.get(repo.full_name)
    if prev_skip is not None and prev_skip["updated_at"] == updated_at.timestamp():
        # keep old data, it hasn't changed
        logging.info("Repo hasn't changed, skipping again based on old data.")
        skips.append(prev_skip)
        continue

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        if release is not None:
            # download release tag (use hardcoded url, because repo.tarball_url can sometimes
            # cause ambiguity errors if a branch is called the same as the release).
            tarball_url = f"https://github.com/{repo.full_name}/tarball/refs/tags/{release.tag_name}"
            get_tarfile = lambda: tarfile.open(
                fileobj=urllib.request.urlopen(tarball_url), mode="r|gz"
            )
            root_dir = get_tarfile().getmembers()[0].name
            get_tarfile().extractall(path=tmp)
            tmp /= root_dir
        else:
            # no latest release, clone main branch
            try:
                gitrepo = git.Repo.clone_from(repo.clone_url, str(tmp), depth=1)
            except git.GitCommandError:
                log_skip("error cloning repository")
                register_skip(repo)
                continue

        workflow = tmp / "workflow"
        if not workflow.exists():
            workflow = tmp

        rules = workflow / "rules"

        # catalog settings
        settings = None
        settings_file = tmp / ".snakemake-workflow-catalog.yml"
        if settings_file.exists():
            with open(settings_file) as settings_file:
                try:
                    settings = yaml.load(settings_file, yaml.SafeLoader)
                except yaml.scanner.ScannerError as e:
                    logging.info(
                        "No standardized usage possible because "
                        "there was an error parsing "
                        ".snakemake-workflow-catalog.yml:\n{}".format(e)
                    )

        linting = None
        formatting = None

        # config readme
        config_readme = None
        config_readme_path = tmp / "config" / "README.md"
        if config_readme_path.exists():
            with open(config_readme_path, "r") as f:
                config_readme = f.read()

    call_rate_limit_aware(
        lambda: repos.append(
            Repo(repo, linting, formatting, config_readme, settings).__dict__
        )
    )

if test_repo is None:
    # Now add all old repos that haven't been covered by the current search.
    # This is necessary because Github limits search queries to 1000 items,
    # and we always use the 1000 with the most recent changes.

    def add_old(old_repos, current_repos, check_existence=True):
        visited = set(repo["full_name"] for repo in current_repos)
        old_repos = [
            repo
            for repo_name, repo in old_repos.items()
            if (not check_existence or check_repo_exists(g, repo_name))
            and repo_name not in visited
        ]
        current_repos.extend(old_repos)

    logging.info("Adding all old repos not covered by the current query.")
    add_old(previous_repos, repos)
    logging.info("Adding all old skipped repos not covered by the current query.")
    add_old(previous_skips, skips)

    logging.info("Processed all available repositories.")
    if len(repos) < (len(previous_repos) / 2.0):
        raise RuntimeError(
            "Previous repos have been twice as big, "
            "likely something went wrong in the github search, aborting."
        )
    store_data()
