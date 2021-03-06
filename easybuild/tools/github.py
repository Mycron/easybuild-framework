##
# Copyright 2012-2019 Ghent University
#
# This file is part of EasyBuild,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/easybuilders/easybuild
#
# EasyBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with EasyBuild.  If not, see <http://www.gnu.org/licenses/>.
##
"""
Utility module for working with github

:author: Jens Timmerman (Ghent University)
:author: Kenneth Hoste (Ghent University)
:author: Toon Willems (Ghent University)
"""
import base64
import copy
import getpass
import glob
import os
import random
import re
import socket
import sys
import tempfile
import time
from datetime import datetime, timedelta
from distutils.version import LooseVersion

from easybuild.base import fancylogger
from easybuild.framework.easyconfig.easyconfig import EASYCONFIGS_ARCHIVE_DIR
from easybuild.framework.easyconfig.easyconfig import copy_easyconfigs, copy_patch_files, process_easyconfig
from easybuild.framework.easyconfig.parser import EasyConfigParser
from easybuild.tools.build_log import EasyBuildError, print_msg, print_warning
from easybuild.tools.config import build_option
from easybuild.tools.filetools import apply_patch, copy_dir, det_patched_files, download_file, extract_file
from easybuild.tools.filetools import mkdir, read_file, symlink, which, write_file
from easybuild.tools.py2vs3 import HTTPError, URLError, ascii_letters, urlopen
from easybuild.tools.systemtools import UNKNOWN, get_tool_version
from easybuild.tools.utilities import nub, only_if_module_is_available


_log = fancylogger.getLogger('github', fname=False)


try:
    import keyring
    HAVE_KEYRING = True
except ImportError as err:
    _log.warning("Failed to import 'keyring' Python module: %s" % err)
    HAVE_KEYRING = False

try:
    from easybuild.base.rest import RestClient
    HAVE_GITHUB_API = True
except ImportError as err:
    _log.warning("Failed to import from 'easybuild.base.rest' Python module: %s" % err)
    HAVE_GITHUB_API = False

try:
    import git
    from git import GitCommandError
except ImportError as err:
    _log.warning("Failed to import 'git' Python module: %s", err)


GITHUB_URL = 'https://github.com'
GITHUB_API_URL = 'https://api.github.com'
GITHUB_DIR_TYPE = u'dir'
GITHUB_EB_MAIN = 'easybuilders'
GITHUB_EASYCONFIGS_REPO = 'easybuild-easyconfigs'
GITHUB_DEVELOP_BRANCH = 'develop'
GITHUB_FILE_TYPE = u'file'
GITHUB_PR_STATE_OPEN = 'open'
GITHUB_PR_STATES = [GITHUB_PR_STATE_OPEN, 'closed', 'all']
GITHUB_PR_ORDER_CREATED = 'created'
GITHUB_PR_ORDERS = [GITHUB_PR_ORDER_CREATED, 'updated', 'popularity', 'long-running']
GITHUB_PR_DIRECTION_DESC = 'desc'
GITHUB_PR_DIRECTIONS = ['asc', GITHUB_PR_DIRECTION_DESC]
GITHUB_MAX_PER_PAGE = 100
GITHUB_MERGEABLE_STATE_CLEAN = 'clean'
GITHUB_PR = 'pull'
GITHUB_RAW = 'https://raw.githubusercontent.com'
GITHUB_STATE_CLOSED = 'closed'
HTTP_STATUS_OK = 200
HTTP_STATUS_CREATED = 201
HTTP_STATUS_NO_CONTENT = 204
KEYRING_GITHUB_TOKEN = 'github_token'
URL_SEPARATOR = '/'

VALID_CLOSE_PR_REASONS = {
    'archived': 'uses an archived toolchain',
    'inactive': 'no activity for > 6 months',
    'obsolete': 'obsoleted by more recent PRs',
    'retest': 'closing and reopening to trigger tests',
}


class Githubfs(object):
    """This class implements some higher level functionality on top of the Github api"""

    def __init__(self, githubuser, reponame, branchname="master", username=None, password=None, token=None):
        """Construct a new githubfs object
        :param githubuser: the github user's repo we want to use.
        :param reponame: The name of the repository we want to use.
        :param branchname: Then name of the branch to use (defaults to master)
        :param username: (optional) your github username.
        :param password: (optional) your github password.
        :param token:    (optional) a github api token.
        """
        if token is None:
            token = fetch_github_token(username)
        self.log = fancylogger.getLogger(self.__class__.__name__, fname=False)
        self.gh = RestClient(GITHUB_API_URL, username=username, password=password, token=token)
        self.githubuser = githubuser
        self.reponame = reponame
        self.branchname = branchname

    @staticmethod
    def join(*args):
        """This method joins 'paths' inside a github repository"""
        args = [x for x in args if x]
        return URL_SEPARATOR.join(args)

    def get_repo(self):
        """Returns the repo as a Github object (from agithub)"""
        return self.gh.repos[self.githubuser][self.reponame]

    def get_path(self, path):
        """returns the path as a Github object (from agithub)"""
        endpoint = self.get_repo()['contents']
        if path:
            for subpath in path.split(URL_SEPARATOR):
                endpoint = endpoint[subpath]
        return endpoint

    @staticmethod
    def isdir(githubobj):
        """Check if this path points to a directory"""
        if isinstance(githubobj, (list, tuple)):
            return True
        else:
            try:
                return githubobj['type'] == GITHUB_DIR_TYPE
            except Exception:
                return False

    @staticmethod
    def isfile(githubobj):
        """Check if this path points to a file"""
        try:
            return githubobj['type'] == GITHUB_FILE_TYPE
        except Exception:
            return False

    def listdir(self, path):
        """List the contents of a directory"""
        path = self.get_path(path)
        listing = path.get(ref=self.branchname)
        self.log.debug("listdir response: %s" % str(listing))
        if listing[0] == 200:
            return listing[1]
        else:
            self.log.warning("error: %s" % str(listing))
            raise EasyBuildError("Invalid response from github (I/O error)")

    def walk(self, top=None, topdown=True):
        """
        Walk the github repo in an os.walk like fashion.
        """
        isdir, listdir = self.isdir, self.listdir

        # If this fails we blow up, since permissions on a github repo are recursive anyway.j
        githubobjs = listdir(top)
        # listdir works with None, but we want to show a decent 'root dir' name
        dirs, nondirs = [], []
        for githubobj in githubobjs:
            if isdir(githubobj):
                dirs.append(str(githubobj['name']))
            else:
                nondirs.append(str(githubobj['name']))

        if topdown:
            yield top, dirs, nondirs

        for name in dirs:
            new_path = self.join(top, name)
            for x in self.walk(new_path, topdown):
                yield x
        if not topdown:
            yield top, dirs, nondirs

    def read(self, path, api=True):
        """Read the contents of a file and return it
        Or, if api=False it will download the file and return the location of the downloaded file"""
        # we don't need use the api for this, but can also use raw.github.com
        # https://raw.github.com/easybuilders/easybuild/master/README.rst
        if not api:
            outfile = tempfile.mkstemp()[1]
            url = '/'.join([GITHUB_RAW, self.githubuser, self.reponame, self.branchname, path])
            download_file(os.path.basename(path), url, outfile)
            return outfile
        else:
            obj = self.get_path(path).get(ref=self.branchname)[1]
            if not self.isfile(obj):
                raise GithubError("Error: not a valid file: %s" % str(obj))
            return base64.b64decode(obj['content'])


class GithubError(Exception):
    """Error raised by the Githubfs"""
    pass


def github_api_get_request(request_f, github_user=None, token=None, **kwargs):
    """
    Helper method, for performing get requests to GitHub API.
    :param request_f: function that should be called to compose request, providing a RestClient instance
    :param github_user: GitHub user name (to try and obtain matching GitHub token if none is provided)
    :param token: GitHub token to use
    :return: tuple with return status and data
    """
    if github_user is None:
        github_user = build_option('github_user')

    if token is None:
        token = fetch_github_token(github_user)

    url = request_f(RestClient(GITHUB_API_URL, username=github_user, token=token))

    try:
        status, data = url.get(**kwargs)
    except socket.gaierror as err:
        _log.warning("Error occurred while performing get request: %s", err)
        status, data = 0, None

    _log.debug("get request result for %s: status: %d, data: %s", url, status, data)
    return (status, data)


def github_api_put_request(request_f, github_user=None, token=None, **kwargs):
    """
    Helper method, for performing put requests to GitHub API.
    :param request_f: function that should be called to compose request, providing a RestClient instance
    :param github_user: GitHub user name (to try and obtain matching GitHub token if none is provided)
    :param token: GitHub token to use
    :return: tuple with return status and data
    """
    if github_user is None:
        github_user = build_option('github_user')

    if token is None:
        token = fetch_github_token(github_user)

    url = request_f(RestClient(GITHUB_API_URL, username=github_user, token=token))

    try:
        status, data = url.put(**kwargs)
    except socket.gaierror as err:
        _log.warning("Error occurred while performing put request: %s", err)
        status, data = 0, {'message': err}

    if status == 200:
        _log.info("Put request successful: %s", data['message'])
    elif status in [405, 409]:
        raise EasyBuildError("FAILED: %s", data['message'])
    else:
        raise EasyBuildError("FAILED: %s", data.get('message', "(unknown reason)"))

    _log.debug("get request result for %s: status: %d, data: %s", url, status, data)
    return (status, data)


def fetch_latest_commit_sha(repo, account, branch='master', github_user=None, token=None):
    """
    Fetch latest SHA1 for a specified repository and branch.
    :param repo: GitHub repository
    :param account: GitHub account
    :param branch: branch to fetch latest SHA1 for
    :param github_user: name of GitHub user to use
    :param token: GitHub token to use
    :return: latest SHA1
    """
    status, data = github_api_get_request(lambda x: x.repos[account][repo].branches,
                                          github_user=github_user, token=token, per_page=GITHUB_MAX_PER_PAGE)
    if status != HTTP_STATUS_OK:
        raise EasyBuildError("Failed to get latest commit sha for branch %s from %s/%s (status: %d %s)",
                             branch, account, repo, status, data)

    res = None
    for entry in data:
        if entry[u'name'] == branch:
            res = entry['commit']['sha']
            break

    if res is None:
        error_msg = "No branch with name %s found in repo %s/%s" % (branch, account, repo)
        if len(data) >= GITHUB_MAX_PER_PAGE:
            error_msg += "; only %d branches were checked (too many branches in %s/%s?)" % (len(data), account, repo)
        raise EasyBuildError(error_msg + ': ' + ', '.join([x[u'name'] for x in data]))

    return res


def download_repo(repo=GITHUB_EASYCONFIGS_REPO, branch='master', account=GITHUB_EB_MAIN, path=None, github_user=None):
    """
    Download entire GitHub repo as a tar.gz archive, and extract it into specified path.
    :param repo: repo to download
    :param branch: branch to download
    :param account: GitHub account to download repo from
    :param path: path to extract to
    :param github_user: name of GitHub user to use
    """
    # make sure path exists, create it if necessary
    if path is None:
        path = tempfile.mkdtemp()

    # add account subdir
    path = os.path.join(path, account)
    mkdir(path, parents=True)

    extracted_dir_name = '%s-%s' % (repo, branch)
    base_name = '%s.tar.gz' % branch
    latest_commit_sha = fetch_latest_commit_sha(repo, account, branch, github_user=github_user)

    expected_path = os.path.join(path, extracted_dir_name)
    latest_sha_path = os.path.join(expected_path, 'latest-sha')

    # check if directory already exists, don't download if 'latest-sha' file indicates that it's up to date
    if os.path.exists(latest_sha_path):
        sha = read_file(latest_sha_path).split('\n')[0].rstrip()
        if latest_commit_sha == sha:
            _log.debug("Not redownloading %s/%s as it already exists: %s" % (account, repo, expected_path))
            return expected_path

    url = URL_SEPARATOR.join([GITHUB_URL, account, repo, 'archive', base_name])

    target_path = os.path.join(path, base_name)
    _log.debug("downloading repo %s/%s as archive from %s to %s" % (account, repo, url, target_path))
    download_file(base_name, url, target_path, forced=True)
    _log.debug("%s downloaded to %s, extracting now" % (base_name, path))

    extracted_path = os.path.join(extract_file(target_path, path, forced=True), extracted_dir_name)

    # check if extracted_path exists
    if not os.path.isdir(extracted_path):
        raise EasyBuildError("%s should exist and contain the repo %s at branch %s", extracted_path, repo, branch)

    write_file(latest_sha_path, latest_commit_sha, forced=True)

    _log.debug("Repo %s at branch %s extracted into %s" % (repo, branch, extracted_path))
    return extracted_path


def fetch_easyconfigs_from_pr(pr, path=None, github_user=None):
    """Fetch patched easyconfig files for a particular PR."""

    if github_user is None:
        github_user = build_option('github_user')
    if path is None:
        path = build_option('pr_path')

    if path is None:
        path = tempfile.mkdtemp()
    else:
        # make sure path exists, create it if necessary
        mkdir(path, parents=True)

    github_account = build_option('pr_target_account')
    github_repo = GITHUB_EASYCONFIGS_REPO

    _log.debug("Fetching easyconfigs from %s/%s PR #%s into %s", github_account, github_repo, pr, path)
    pr_data, _ = fetch_pr_data(pr, github_account, github_repo, github_user)

    pr_merged = pr_data['merged']
    pr_closed = pr_data['state'] == GITHUB_STATE_CLOSED and not pr_merged

    pr_target_branch = pr_data['base']['ref']
    _log.info("Target branch for PR #%s: %s", pr, pr_target_branch)

    # download target branch of PR so we can try and apply the PR patch on top of it
    repo_target_branch = download_repo(repo=github_repo, account=github_account, branch=pr_target_branch,
                                       github_user=github_user)

    # determine list of changed files via diff
    diff_fn = os.path.basename(pr_data['diff_url'])
    diff_filepath = os.path.join(path, diff_fn)
    download_file(diff_fn, pr_data['diff_url'], diff_filepath, forced=True)
    diff_txt = read_file(diff_filepath)
    _log.debug("Diff for PR #%s:\n%s", pr, diff_txt)

    patched_files = det_patched_files(txt=diff_txt, omit_ab_prefix=True, github=True, filter_deleted=True)
    _log.debug("List of patched files for PR #%s: %s", pr, patched_files)

    final_path = None

    # try to apply PR patch on top of target branch, unless the PR is closed or already merged
    if pr_merged:
        _log.info("PR is already merged, so using current version of PR target branch")
        final_path = repo_target_branch

    elif not pr_closed:
        try:
            _log.debug("Trying to apply PR patch %s to %s...", diff_filepath, repo_target_branch)
            apply_patch(diff_filepath, repo_target_branch, use_git_am=True)
            _log.info("Using %s which included PR patch to test PR #%s", repo_target_branch, pr)
            final_path = repo_target_branch

        except EasyBuildError as err:
            _log.warning("Ignoring problem that occured when applying PR patch: %s", err)

    if final_path is None:

        if pr_closed:
            print_warning("Using easyconfigs from closed PR #%s" % pr)

        # obtain most recent version of patched files
        for patched_file in patched_files:
            # path to patch file, incl. subdir it is in
            fn = os.path.sep.join(patched_file.split(os.path.sep)[-3:])
            sha = pr_data['head']['sha']
            full_url = URL_SEPARATOR.join([GITHUB_RAW, github_account, github_repo, sha, patched_file])
            _log.info("Downloading %s from %s", fn, full_url)
            download_file(fn, full_url, path=os.path.join(path, fn), forced=True)

        final_path = path

    # symlink directories into expected place if they're not there yet
    if final_path != path:
        dirpath = os.path.join(final_path, 'easybuild', 'easyconfigs')
        for eb_dir in os.listdir(dirpath):
            symlink(os.path.join(dirpath, eb_dir), os.path.join(path, os.path.basename(eb_dir)))

    # sanity check: make sure all patched files are downloaded
    ec_files = []
    for patched_file in [f for f in patched_files if not f.startswith('test/')]:
        fn = os.path.sep.join(patched_file.split(os.path.sep)[-3:])
        full_path = os.path.join(path, fn)
        if os.path.exists(full_path):
            ec_files.append(full_path)
        else:
            raise EasyBuildError("Couldn't find path to patched file %s", full_path)

    return ec_files


def create_gist(txt, fn, descr=None, github_user=None, github_token=None):
    """Create a gist with the provided text."""
    if descr is None:
        descr = "(none)"

    if github_token is None:
        github_token = fetch_github_token(github_user)

    body = {
        "description": descr,
        "public": True,
        "files": {
            fn: {
                "content": txt,
            }
        }
    }
    g = RestClient(GITHUB_API_URL, username=github_user, token=github_token)
    status, data = g.gists.post(body=body)

    if status != HTTP_STATUS_CREATED:
        raise EasyBuildError("Failed to create gist; status %s, data: %s", status, data)

    return data['html_url']


def delete_gist(gist_id, github_user=None, github_token=None):
    """Delete gist with specified ID."""

    if github_token is None:
        github_token = fetch_github_token(github_user)

    gh = RestClient(GITHUB_API_URL, username=github_user, token=github_token)
    status, data = gh.gists[gist_id].delete()

    if status != HTTP_STATUS_NO_CONTENT:
        raise EasyBuildError("Failed to delete gist with ID %s: status %s, data: %s", status, data)


def post_comment_in_issue(issue, txt, account=GITHUB_EB_MAIN, repo=GITHUB_EASYCONFIGS_REPO, github_user=None):
    """Post a comment in the specified PR."""
    if not isinstance(issue, int):
        try:
            issue = int(issue)
        except ValueError as err:
            raise EasyBuildError("Failed to parse specified pull request number '%s' as an int: %s; ", issue, err)

    dry_run = build_option('dry_run') or build_option('extended_dry_run')

    msg = "Adding comment to %s issue #%s: '%s'" % (repo, issue, txt)
    if dry_run:
        msg = "[DRY RUN] " + msg
    print_msg(msg, log=_log, prefix=False)

    if not dry_run:
        github_token = fetch_github_token(github_user)

        g = RestClient(GITHUB_API_URL, username=github_user, token=github_token)
        pr_url = g.repos[account][repo].issues[issue]

        status, data = pr_url.comments.post(body={'body': txt})
        if not status == HTTP_STATUS_CREATED:
            raise EasyBuildError("Failed to create comment in PR %s#%d; status %s, data: %s", repo, issue, status, data)


def init_repo(path, repo_name, silent=False):
    """
    Initialize a new Git repository at the specified location.

    :param path: location where Git repository should be initialized
    :param repo_name: name of Git repository
    :param silent: keep quiet (don't print any messages)
    """
    repo_path = os.path.join(path, repo_name)

    if not os.path.exists(repo_path):
        mkdir(repo_path, parents=True)

    # clone repo in git_working_dirs_path to repo_path
    git_working_dirs_path = build_option('git_working_dirs_path')
    if git_working_dirs_path:
        workdir = os.path.join(git_working_dirs_path, repo_name)
        if os.path.exists(workdir):
            print_msg("cloning git repo from %s..." % workdir, silent=silent)
            try:
                workrepo = git.Repo(workdir)
                workrepo.clone(repo_path)
            except GitCommandError as err:
                raise EasyBuildError("Failed to clone git repo at %s: %s", workdir, err)

    # initalize repo in repo_path
    try:
        repo = git.Repo.init(repo_path)
    except GitCommandError as err:
        raise EasyBuildError("Failed to init git repo at %s: %s", repo_path, err)

    _log.debug("temporary git working directory ready at %s", repo_path)

    return repo


def setup_repo_from(git_repo, github_url, target_account, branch_name, silent=False):
    """
    Set up repository by checking out specified branch from repository at specified URL.

    :param git_repo: git.Repo instance
    :param github_url: URL to GitHub repository
    :param target_account: name of GitHub account that owns GitHub repository at specified URL
    :param branch_name: name of branch to check out
    :param silent: keep quiet (don't print any messages)
    """
    _log.debug("Cloning from %s", github_url)

    # salt to use for names of remotes/branches that are created
    salt = ''.join(random.choice(ascii_letters) for _ in range(5))

    remote_name = 'pr_target_account_%s_%s' % (target_account, salt)

    origin = git_repo.create_remote(remote_name, github_url)
    if not origin.exists():
        raise EasyBuildError("%s does not exist?", github_url)

    # git fetch
    # can't use --depth to only fetch a shallow copy, since pushing to another repo from a shallow copy doesn't work
    print_msg("fetching branch '%s' from %s..." % (branch_name, github_url), silent=silent)
    try:
        res = origin.fetch()
    except GitCommandError as err:
        raise EasyBuildError("Failed to fetch branch '%s' from %s: %s", branch_name, github_url, err)
    if res:
        if res[0].flags & res[0].ERROR:
            raise EasyBuildError("Fetching branch '%s' from remote %s failed: %s", branch_name, origin, res[0].note)
        else:
            _log.debug("Fetched branch '%s' from remote %s (note: %s)", branch_name, origin, res[0].note)
    else:
        raise EasyBuildError("Fetching branch '%s' from remote %s failed: empty result", branch_name, origin)

    # git checkout -b <branch>; git pull
    if hasattr(origin.refs, branch_name):
        origin_branch = getattr(origin.refs, branch_name)
    else:
        raise EasyBuildError("Branch '%s' not found at %s", branch_name, github_url)

    _log.debug("Checking out branch '%s' from remote %s", branch_name, github_url)
    try:
        origin_branch.checkout(b=branch_name)
    except GitCommandError as err:
        alt_branch = '%s_%s' % (branch_name, salt)
        _log.debug("Trying to work around checkout error ('%s') by using different branch name '%s'", err, alt_branch)
        try:
            origin_branch.checkout(b=alt_branch, force=True)
        except GitCommandError as err:
            raise EasyBuildError("Failed to check out branch '%s' from repo at %s: %s", alt_branch, github_url, err)

    return remote_name


def setup_repo(git_repo, target_account, target_repo, branch_name, silent=False, git_only=False):
    """
    Set up repository by checking out specified branch for specfied GitHub account/repository.

    :param git_repo: git.Repo instance
    :param target_account: name of GitHub account that owns GitHub repository
    :param target_repo: name of GitHib repository
    :param branch_name: name of branch to check out
    :param silent: keep quiet (don't print any messages)
    :param git_only: only use git@github.com repo URL, skip trying https://github.com first
    """
    tmpl_github_urls = [
        'git@github.com:%s/%s.git',
    ]
    if not git_only:
        tmpl_github_urls.insert(0, 'https://github.com/%s/%s.git')

    res = None
    errors = []
    for tmpl_github_url in tmpl_github_urls:
        github_url = tmpl_github_url % (target_account, target_repo)
        try:
            res = setup_repo_from(git_repo, github_url, target_account, branch_name, silent=silent)
            break

        except EasyBuildError as err:
            errors.append("Checking out branch '%s' from %s failed: %s" % (branch_name, github_url, err))

    if res:
        return res
    else:
        raise EasyBuildError('\n'.join(errors))


@only_if_module_is_available('git', pkgname='GitPython')
def _easyconfigs_pr_common(paths, ecs, start_branch=None, pr_branch=None, start_account=None, commit_msg=None):
    """
    Common code for new_pr and update_pr functions:
    * check whether all supplied paths point to existing files
    * create temporary clone of target git repository
    * fetch/checkout specified starting branch
    * copy files to right location
    * stage/commit all files in PR branch
    * push PR branch to GitHub (to account specified by --github-user)

    :param paths: paths to categorized lists of files (easyconfigs, files to delete, patches)
    :param ecs: list of parsed easyconfigs, incl. for dependencies (if robot is enabled)
    :param start_branch: name of branch to use as base for PR
    :param pr_branch: name of branch to push to GitHub
    :param start_account: name of GitHub account to use as base for PR
    :param commit_msg: commit message to use
    """
    # we need files to create the PR with
    non_existing_paths = []
    ec_paths = []
    if paths['easyconfigs']:
        for path in paths['easyconfigs']:
            if not os.path.exists(path):
                non_existing_paths.append(path)
            else:
                ec_paths.append(path)

        if non_existing_paths:
            raise EasyBuildError("One or more non-existing paths specified: %s", ', '.join(non_existing_paths))

    if not any(paths.values()):
        raise EasyBuildError("No paths specified")

    pr_target_repo = build_option('pr_target_repo')

    # initialize repository
    git_working_dir = tempfile.mkdtemp(prefix='git-working-dir')
    git_repo = init_repo(git_working_dir, pr_target_repo)
    repo_path = os.path.join(git_working_dir, pr_target_repo)

    if pr_target_repo != GITHUB_EASYCONFIGS_REPO:
        raise EasyBuildError("Don't know how to create/update a pull request to the %s repository", pr_target_repo)

    if start_branch is None:
        # if start branch is not specified, we're opening a new PR
        # account to use is determined by active EasyBuild configuration (--github-org or --github-user)
        target_account = build_option('github_org') or build_option('github_user')
        # if branch to start from is specified, we're updating an existing PR
        start_branch = build_option('pr_target_branch')
    else:
        # account to target is the one that owns the branch used to open PR
        # (which may be different from account used to push update!)
        target_account = start_account

    # set up repository
    setup_repo(git_repo, start_account, pr_target_repo, start_branch)

    _log.debug("git status: %s", git_repo.git.status())

    # copy easyconfig files to right place
    target_dir = os.path.join(git_working_dir, pr_target_repo)
    print_msg("copying easyconfigs to %s..." % target_dir)
    file_info = copy_easyconfigs(ec_paths, target_dir)

    # figure out commit message to use
    if commit_msg:
        cnt = len(file_info['paths_in_repo'])
        _log.debug("Using specified commit message for all %d new/modified easyconfigs at once: %s", cnt, commit_msg)
    elif all(file_info['new']) and not paths['files_to_delete']:
        # automagically derive meaningful commit message if all easyconfig files are new
        commit_msg = "adding easyconfigs: %s" % ', '.join(os.path.basename(p) for p in file_info['paths_in_repo'])
        if paths['patch_files']:
            commit_msg += " and patches: %s" % ', '.join(os.path.basename(p) for p in paths['patch_files'])
    else:
        raise EasyBuildError("A meaningful commit message must be specified via --pr-commit-msg when "
                             "modifying/deleting easyconfigs")

    # figure out to which software name patches relate, and copy them to the right place
    if paths['patch_files']:
        patch_specs = det_patch_specs(paths['patch_files'], file_info, [target_dir])

        print_msg("copying patch files to %s..." % target_dir)
        patch_info = copy_patch_files(patch_specs, target_dir)

    # determine path to files to delete (if any)
    deleted_paths = []
    for fn in paths['files_to_delete']:
        fullpath = os.path.join(repo_path, fn)
        if os.path.exists(fullpath):
            deleted_paths.append(fullpath)
        else:
            # if no existing relative path is specified, assume just the easyconfig file name is provided
            hits = glob.glob(os.path.join(repo_path, 'easybuild', 'easyconfigs', '*', '*', fn))
            if len(hits) == 1:
                deleted_paths.append(hits[0])
            else:
                raise EasyBuildError("Path doesn't exist or file to delete isn't found in target branch: %s", fn)

    dep_info = {
        'ecs': [],
        'paths_in_repo': [],
        'new': [],
    }

    # include missing easyconfigs for dependencies, if robot is enabled
    if ecs is not None:

        abs_paths = [os.path.realpath(os.path.abspath(path)) for path in ec_paths]
        dep_paths = [ec['spec'] for ec in ecs if os.path.realpath(ec['spec']) not in abs_paths]
        _log.info("Paths to easyconfigs for missing dependencies: %s", dep_paths)
        all_dep_info = copy_easyconfigs(dep_paths, target_dir)

        # only consider new easyconfig files for dependencies (not updated ones)
        for idx in range(len(all_dep_info['ecs'])):
            if all_dep_info['new'][idx]:
                for key in dep_info:
                    dep_info[key].append(all_dep_info[key][idx])

    # checkout target branch
    if pr_branch is None:
        if ec_paths:
            label = file_info['ecs'][0].name + re.sub('[.-]', '', file_info['ecs'][0].version)
        else:
            label = ''.join(random.choice(ascii_letters) for _ in range(10))
        pr_branch = '%s_new_pr_%s' % (time.strftime("%Y%m%d%H%M%S"), label)

    # create branch to commit to and push;
    # use force to avoid errors if branch already exists (OK since this is a local temporary copy of the repo)
    git_repo.create_head(pr_branch, force=True).checkout()
    _log.info("New branch '%s' created to commit files to", pr_branch)

    # stage
    _log.debug("Staging all %d new/modified easyconfigs", len(file_info['paths_in_repo']))
    git_repo.index.add(file_info['paths_in_repo'])
    git_repo.index.add(dep_info['paths_in_repo'])

    if paths['patch_files']:
        _log.debug("Staging all %d new/modified patch files", len(patch_info['paths_in_repo']))
        git_repo.index.add(patch_info['paths_in_repo'])

    # stage deleted files
    if deleted_paths:
        git_repo.index.remove(deleted_paths)

    # overview of modifications
    if build_option('extended_dry_run'):
        print_msg("\nFull patch:\n", log=_log, prefix=False)
        print_msg(git_repo.git.diff(cached=True) + '\n', log=_log, prefix=False)

    diff_stat = git_repo.git.diff(cached=True, stat=True)
    if not diff_stat:
        raise EasyBuildError("No changed files found when comparing to current develop branch. "
                             "Refused to make empty pull request.")

    # commit
    git_repo.index.commit(commit_msg)

    push_branch_to_github(git_repo, target_account, pr_target_repo, pr_branch)

    return file_info, deleted_paths, git_repo, pr_branch, diff_stat


def create_remote(git_repo, account, repo, https=False):
    """
    Create remote in specified git working directory for specified account & repository.

    :param git_repo: git.Repo instance to use (after init_repo & setup_repo)
    :param account: GitHub account name
    :param repo: repository name
    :param https: use https:// URL rather than git@
    """

    if https:
        github_url = 'https://github.com/%s/%s.git' % (account, repo)
    else:
        github_url = 'git@github.com:%s/%s.git' % (account, repo)

    salt = ''.join(random.choice(ascii_letters) for _ in range(5))
    remote_name = 'github_%s_%s' % (account, salt)

    try:
        remote = git_repo.create_remote(remote_name, github_url)
    except GitCommandError as err:
        raise EasyBuildError("Failed to create remote %s for %s: %s", remote_name, github_url, err)

    return remote


def push_branch_to_github(git_repo, target_account, target_repo, branch):
    """
    Push specified branch to GitHub from specified git repository.

    :param git_repo: git.Repo instance to use (after init_repo & setup_repo)
    :param target_account: GitHub account name
    :param target_repo: repository name
    :param branch: name of branch to push
    """

    # push to GitHub
    remote = create_remote(git_repo, target_account, target_repo)

    dry_run = build_option('dry_run') or build_option('extended_dry_run')

    github_url = 'git@github.com:%s/%s.git' % (target_account, target_repo)

    push_branch_msg = "pushing branch '%s' to remote '%s' (%s)" % (branch, remote.name, github_url)
    if dry_run:
        print_msg(push_branch_msg + ' [DRY RUN]', log=_log)
    else:
        print_msg(push_branch_msg, log=_log)
        try:
            res = remote.push(branch)
        except GitCommandError as err:
            raise EasyBuildError("Failed to push branch '%s' to GitHub (%s): %s", branch, github_url, err)

        if res:
            if res[0].ERROR & res[0].flags:
                raise EasyBuildError("Pushing branch '%s' to remote %s (%s) failed: %s",
                                     branch, remote, github_url, res[0].summary)
            else:
                _log.debug("Pushed branch %s to remote %s (%s): %s", branch, remote, github_url, res[0].summary)
        else:
            raise EasyBuildError("Pushing branch '%s' to remote %s (%s) failed: empty result",
                                 branch, remote, github_url)


def is_patch_for(patch_name, ec):
    """Check whether specified patch matches any patch in the provided EasyConfig instance."""
    res = False

    patches = copy.copy(ec['patches'])

    for ext in ec['exts_list']:
        if isinstance(ext, (list, tuple)) and len(ext) == 3 and isinstance(ext[2], dict):
            ext_options = ext[2]
            patches.extend(ext_options.get('patches', []))

    for patch in patches:
        if isinstance(patch, (tuple, list)):
            patch = patch[0]
        if patch == patch_name:
            res = True
            break

    return res


def det_patch_specs(patch_paths, file_info, ec_dirs):
    """ Determine software names for patch files """
    print_msg("determining software names for patch files...")
    patch_specs = []
    for patch_path in patch_paths:
        soft_name = None
        patch_file = os.path.basename(patch_path)

        # consider patch lists of easyconfigs being provided
        for ec in file_info['ecs']:
            if is_patch_for(patch_file, ec):
                soft_name = ec['name']
                break

        if soft_name:
            patch_specs.append((patch_path, soft_name))
        else:
            # fall back on scanning all eb files for patches
            print("Matching easyconfig for %s not found on the first try:" % patch_path)
            print("scanning all easyconfigs to determine where patch file belongs (this may take a while)...")
            soft_name = find_software_name_for_patch(patch_file, ec_dirs)
            if soft_name:
                patch_specs.append((patch_path, soft_name))
            else:
                # still nothing found
                raise EasyBuildError("Failed to determine software name to which patch file %s relates", patch_path)

    return patch_specs


def find_software_name_for_patch(patch_name, ec_dirs):
    """
    Scan all easyconfigs in the robot path(s) to determine which software a patch file belongs to

    :param patch_name: name of the patch file
    :param ecs_dirs: list of directories to consider when looking for easyconfigs
    :return: name of the software that this patch file belongs to (if found)
    """

    soft_name = None

    all_ecs = []
    for ec_dir in ec_dirs:
        for (dirpath, _, filenames) in os.walk(ec_dir):
            for fn in filenames:
                if fn != 'TEMPLATE.eb' and not fn.endswith('.py'):
                    path = os.path.join(dirpath, fn)
                    rawtxt = read_file(path)
                    if 'patches' in rawtxt:
                        all_ecs.append(path)

    nr_of_ecs = len(all_ecs)
    for idx, path in enumerate(all_ecs):
        if soft_name:
            break
        rawtxt = read_file(path)
        try:
            ecs = process_easyconfig(path, validate=False)
            for ec in ecs:
                if is_patch_for(patch_name, ec['ec']):
                    soft_name = ec['ec']['name']
                    break
        except EasyBuildError as err:
            _log.debug("Ignoring easyconfig %s that fails to parse: %s", path, err)
        sys.stdout.write('\r%s of %s easyconfigs checked' % (idx+1, nr_of_ecs))
        sys.stdout.flush()

    sys.stdout.write('\n')
    return soft_name


def check_pr_eligible_to_merge(pr_data):
    """
    Check whether PR is eligible for merging.

    :param pr_data: PR data obtained through GitHub API
    :return: boolean value indicates whether PR is eligible
    """
    res = True

    def not_eligible(msg):
        """Helper function to warn about PR not being eligible for merging"""
        print_msg("%s => not eligible for merging!" % msg, stderr=True, prefix=False)
        return False

    target = '%s/%s' % (pr_data['base']['repo']['owner']['login'], pr_data['base']['repo']['name'])
    print_msg("Checking eligibility of %s PR #%s for merging..." % (target, pr_data['number']), prefix=False)

    # check target branch, must be 'develop'
    msg_tmpl = "* targets develop branch: %s"
    if pr_data['base']['ref'] == 'develop':
        print_msg(msg_tmpl % 'OK', prefix=False)
    else:
        res = not_eligible(msg_tmpl % "FAILED; found '%s'" % pr_data['base']['ref'])

    # check test suite result, Travis must give green light
    msg_tmpl = "* test suite passes: %s"
    if pr_data['status_last_commit'] == 'success':
        print_msg(msg_tmpl % 'OK', prefix=False)
    elif pr_data['status_last_commit'] == 'pending':
        res = not_eligible(msg_tmpl % "pending...")
    elif pr_data['status_last_commit'] in ['error', 'failure']:
        res = not_eligible(msg_tmpl % "FAILED")
    else:
        res = not_eligible(msg_tmpl % "(result unknown)")

    if pr_data['base']['repo']['name'] == GITHUB_EASYCONFIGS_REPO:
        # check for successful test report (checked in reverse order)
        msg_tmpl = "* last test report is successful: %s"
        test_report_regex = re.compile(r"^Test report by @\S+")
        test_report_found = False
        for comment in pr_data['issue_comments'][::-1]:
            comment = comment['body']
            if test_report_regex.search(comment):
                if 'SUCCESS' in comment:
                    print_msg(msg_tmpl % 'OK', prefix=False)
                    test_report_found = True
                    break
                elif 'FAILED' in comment:
                    res = not_eligible(msg_tmpl % 'FAILED')
                    test_report_found = True
                    break
                else:
                    print_warning("Failed to determine outcome of test report for comment:\n%s" % comment)

        if not test_report_found:
            res = not_eligible(msg_tmpl % "(no test reports found)")

    # check for approved review
    approved_review_by = []
    for review in pr_data['reviews']:
        if review['state'] == 'APPROVED':
            approved_review_by.append(review['user']['login'])

    msg_tmpl = "* approved review: %s"
    if approved_review_by:
        print_msg(msg_tmpl % 'OK (by %s)' % ', '.join(approved_review_by), prefix=False)
    else:
        res = not_eligible(msg_tmpl % 'MISSING')

    # check whether a milestone is set
    msg_tmpl = "* milestone is set: %s"
    if pr_data['milestone']:
        print_msg(msg_tmpl % "OK (%s)" % pr_data['milestone']['title'], prefix=False)
    else:
        res = not_eligible(msg_tmpl % 'no milestone found')

    return res


def reasons_for_closing(pr_data):
    """
    Look for valid reasons to close PR by comparing with existing easyconfigs.
    """

    if pr_data['status_last_commit']:
        print_msg("Status of last commit is %s\n" % pr_data['status_last_commit'].upper(), prefix=False)

    if pr_data['issue_comments']:
        last_comment = pr_data['issue_comments'][-1]
        timestamp = last_comment['updated_at'].replace('T', ' at ')[:-1]
        username = last_comment['user']['login']
        print_msg("Last comment on %s, by %s, was:\n\n%s" % (timestamp, username, last_comment['body']), prefix=False)

    if pr_data['reviews']:
        last_review = pr_data['reviews'][-1]
        timestamp = last_review['submitted_at'].replace('T', ' at ')[:-1]
        username = last_review['user']['login']
        state, body = last_review['state'], last_review['body']
        print_msg("Last reviewed on %s by %s, state %s\n\n%s" % (timestamp, username, state, body), prefix=False)

    possible_reasons = []

    print_msg("No activity since %s" % pr_data['updated_at'].replace('T', ' at ')[:-1], prefix=False)

    # check if PR is inactive for more than 6 months
    last_updated = datetime.strptime(pr_data['updated_at'], "%Y-%m-%dT%H:%M:%SZ")
    if datetime.now() - last_updated > timedelta(days=180):
        possible_reasons.append('inactive')

    robot_paths = build_option('robot_path')

    pr_files = [path for path in fetch_easyconfigs_from_pr(pr_data['number']) if path.endswith('.eb')]

    obsoleted = []
    uses_archived_tc = []
    for pr_file in pr_files:
        pr_ec = EasyConfigParser(pr_file).get_config_dict()
        pr_tc = '%s-%s' % (pr_ec['toolchain']['name'], pr_ec['toolchain']['version'])
        print_msg("* %s-%s" % (pr_ec['name'], pr_ec['version']), prefix=False)
        for robot_path in robot_paths:
            # check if PR easyconfig uses an archived toolchain
            path = os.path.join(robot_path, EASYCONFIGS_ARCHIVE_DIR, pr_tc[0].lower(), pr_tc.split('-')[0])
            for (dirpath, _, filenames) in os.walk(path):
                for fn in filenames:
                    if fn.endswith('.eb'):
                        ec = EasyConfigParser(os.path.join(dirpath, fn)).get_config_dict()
                        if ec.get('easyblock') == 'Toolchain':
                            if 'versionsuffix' in ec:
                                archived_tc = '%s-%s%s' % (ec['name'], ec['version'], ec.get('versionsuffix'))
                            else:
                                archived_tc = '%s-%s' % (ec['name'], ec['version'])
                            if pr_tc == archived_tc:
                                print_msg(" - uses archived toolchain %s" % pr_tc, prefix=False)
                                uses_archived_tc.append(pr_ec)

            # check if there is a newer version of PR easyconfig
            newer_versions = set()
            for (dirpath, _, filenames) in os.walk(os.path.join(robot_path, pr_ec['name'].lower()[0], pr_ec['name'])):
                for fn in filenames:
                    if fn.endswith('.eb'):
                        ec = EasyConfigParser(os.path.join(dirpath, fn)).get_config_dict()
                        if LooseVersion(ec['version']) > LooseVersion(pr_ec['version']):
                            newer_versions.add(ec['version'])

            if newer_versions:
                print_msg(" - found newer versions %s" % ", ".join(sorted(newer_versions)), prefix=False)
                obsoleted.append(pr_ec)

    if uses_archived_tc:
        possible_reasons.append('archived')

    if any([e['name'] in pr_data['title'] for e in obsoleted]):
        possible_reasons.append('obsolete')

    return possible_reasons


def close_pr(pr, motivation_msg=None):
    """
    Close specified pull request

    :param pr: PR number
    :param motivation_msg: string containing motivation for closing the PR
    """
    github_user = build_option('github_user')
    if github_user is None:
        raise EasyBuildError("GitHub user must be specified to use --close-pr")

    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    pr_data, _ = fetch_pr_data(pr, pr_target_account, pr_target_repo, github_user, full=True)

    if pr_data['state'] == GITHUB_STATE_CLOSED:
        raise EasyBuildError("PR #%d from %s/%s is already closed.", pr, pr_target_account, pr_target_repo)

    pr_owner = pr_data['user']['login']
    msg = "\n%s/%s PR #%s was submitted by %s, " % (pr_target_account, pr_target_repo, pr, pr_owner)
    msg += "you are using GitHub account '%s'\n" % github_user
    msg += "\nPR Title: \"%s\"\n" % pr_data['title']
    print_msg(msg, prefix=False)

    dry_run = build_option('dry_run') or build_option('extended_dry_run')

    reopen = motivation_msg == VALID_CLOSE_PR_REASONS['retest']

    if not motivation_msg:
        print_msg("No reason or message specified, looking for possible reasons\n")
        possible_reasons = reasons_for_closing(pr_data)

        if not possible_reasons:
            raise EasyBuildError("No reason specified and none found from PR data, "
                                 "please use --close-pr-reasons or --close-pr-msg")
        else:
            motivation_msg = ", ".join([VALID_CLOSE_PR_REASONS[reason] for reason in possible_reasons])
            print_msg("\nNo reason specified but found possible reasons: %s.\n" % motivation_msg, prefix=False)

    msg = "@%s, this PR is being closed for the following reason(s): %s." % (pr_data['user']['login'], motivation_msg)
    if not reopen:
        msg += "\nPlease don't hesitate to reopen this PR or add a comment if you feel this contribution is still "
        msg += "relevant.\nFor more information on our policy w.r.t. closing PRs, see "
        msg += "https://easybuild.readthedocs.io/en/latest/Contributing.html"
        msg += "#why-a-pull-request-may-be-closed-by-a-maintainer"
    post_comment_in_issue(pr, msg, account=pr_target_account, repo=pr_target_repo, github_user=github_user)

    if dry_run:
        print_msg("[DRY RUN] Closed %s/%s PR #%s" % (pr_target_account, pr_target_repo, pr), prefix=False)
        if reopen:
            print_msg("[DRY RUN] Reopened %s/%s PR #%s" % (pr_target_account, pr_target_repo, pr), prefix=False)
    else:
        github_token = fetch_github_token(github_user)
        if github_token is None:
            raise EasyBuildError("GitHub token for user '%s' must be available to use --close-pr", github_user)
        g = RestClient(GITHUB_API_URL, username=github_user, token=github_token)
        pull_url = g.repos[pr_target_account][pr_target_repo].pulls[pr]
        body = {'state': 'closed'}
        status, data = pull_url.post(body=body)
        if not status == HTTP_STATUS_OK:
            raise EasyBuildError("Failed to close PR #%s; status %s, data: %s", pr, status, data)
        if reopen:
            body = {'state': 'open'}
            status, data = pull_url.post(body=body)
            if not status == HTTP_STATUS_OK:
                raise EasyBuildError("Failed to reopen PR #%s; status %s, data: %s", pr, status, data)


def list_prs(params, per_page=GITHUB_MAX_PER_PAGE, github_user=None):
    """
    List pull requests according to specified selection/order parameters

    :param params: 3-tuple with selection parameters for PRs (<state>, <sort>, <direction>),
                   see https://developer.github.com/v3/pulls/#parameters
    """
    parameters = {
        'state': params[0],
        'sort': params[1],
        'direction': params[2],
        'per_page': per_page,
    }
    print_msg("Listing PRs with parameters: %s" % ', '.join(k + '=' + str(parameters[k]) for k in sorted(parameters)))

    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    pr_data, _ = fetch_pr_data(None, pr_target_account, pr_target_repo, github_user, **parameters)

    lines = []
    for pr in pr_data:
        lines.append("PR #%s: %s" % (pr['number'], pr['title']))

    return '\n'.join(lines)


def merge_pr(pr):
    """
    Merge specified pull request
    """
    github_user = build_option('github_user')
    if github_user is None:
        raise EasyBuildError("GitHub user must be specified to use --merge-pr")

    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    pr_data, pr_url = fetch_pr_data(pr, pr_target_account, pr_target_repo, github_user, full=True)

    msg = "\n%s/%s PR #%s was submitted by %s, " % (pr_target_account, pr_target_repo, pr, pr_data['user']['login'])
    msg += "you are using GitHub account '%s'\n" % github_user
    print_msg(msg, prefix=False)
    if pr_data['user']['login'] == github_user:
        raise EasyBuildError("Please do not merge your own PRs!")

    force = build_option('force')
    dry_run = build_option('dry_run') or build_option('extended_dry_run')

    def merge_url(gh):
        """Utility function to fetch merge URL for a specific PR."""
        return gh.repos[pr_target_account][pr_target_repo].pulls[pr].merge

    if check_pr_eligible_to_merge(pr_data) or force:
        print_msg("\nReview %s merging pull request!\n" % ("OK,", "FAILed, yet forcibly")[force], prefix=False)

        comment = "Going in, thanks @%s!" % pr_data['user']['login']
        post_comment_in_issue(pr, comment, account=pr_target_account, repo=pr_target_repo, github_user=github_user)

        if dry_run:
            print_msg("[DRY RUN] Merged %s/%s pull request #%s" % (pr_target_account, pr_target_repo, pr), prefix=False)
        else:
            body = {
                'commit_message': pr_data['title'],
                'sha': pr_data['head']['sha'],
            }
            github_api_put_request(merge_url, github_user, body=body)
    else:
        print_warning("Review indicates this PR should not be merged (use -f/--force to do so anyway)")


@only_if_module_is_available('git', pkgname='GitPython')
def new_pr(paths, ecs, title=None, descr=None, commit_msg=None):
    """
    Open new pull request using specified files

    :param paths: paths to categorized lists of files (easyconfigs, files to delete, patches)
    :param ecs: list of parsed easyconfigs, incl. for dependencies (if robot is enabled)
    :param title: title to use for pull request
    :param descr: description to use for description
    :param commit_msg: commit message to use
    """
    pr_branch_name = build_option('pr_branch_name')
    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    # collect GitHub info we'll need
    # * GitHub username to push branch to repo
    # * GitHub token to open PR
    github_user = build_option('github_user')
    if github_user is None:
        raise EasyBuildError("GitHub user must be specified to use --new-pr")
    github_account = build_option('github_org') or build_option('github_user')

    github_token = fetch_github_token(github_user)
    if github_token is None:
        raise EasyBuildError("GitHub token for user '%s' must be available to use --new-pr", github_user)

    # create branch, commit files to it & push to GitHub
    file_info, deleted_paths, git_repo, branch, diff_stat = _easyconfigs_pr_common(paths, ecs,
                                                                                   pr_branch=pr_branch_name,
                                                                                   start_account=pr_target_account,
                                                                                   commit_msg=commit_msg)

    # label easyconfigs for new software and/or new easyconfigs for existing software
    labels = []
    if any(file_info['new_folder']):
        labels.append('new')
    if any(file_info['new_file_in_existing_folder']):
        labels.append('update')

    # only use most common toolchain(s) in toolchain label of PR title
    toolchains = ['%(name)s/%(version)s' % ec['toolchain'] for ec in file_info['ecs']]
    toolchains_counted = sorted([(toolchains.count(tc), tc) for tc in nub(toolchains)])
    toolchain_label = ','.join([tc for (cnt, tc) in toolchains_counted if cnt == toolchains_counted[-1][0]])

    # only use most common module class(es) in moduleclass label of PR title
    classes = [ec['moduleclass'] for ec in file_info['ecs']]
    classes_counted = sorted([(classes.count(c), c) for c in nub(classes)])
    class_label = ','.join([tc for (cnt, tc) in classes_counted if cnt == classes_counted[-1][0]])

    if title is None:
        if commit_msg:
            title = commit_msg
        elif file_info['ecs'] and all(file_info['new']) and not deleted_paths:
            # mention software name/version in PR title (only first 3)
            names_and_versions = nub(["%s v%s" % (ec.name, ec.version) for ec in file_info['ecs']])
            if len(names_and_versions) <= 3:
                main_title = ', '.join(names_and_versions)
            else:
                main_title = ', '.join(names_and_versions[:3] + ['...'])

            title = "{%s}[%s] %s" % (class_label, toolchain_label, main_title)

            # if Python is listed as a dependency, then mention Python version(s) in PR title
            pyver = []
            for ec in file_info['ecs']:
                # iterate over all dependencies (incl. build dependencies & multi-deps)
                for dep in ec.dependencies():
                    if dep['name'] == 'Python':
                        # check whether Python is listed as a multi-dep if it's marked as a build dependency
                        if dep['build_only'] and 'Python' not in ec['multi_deps']:
                            continue
                        else:
                            pyver.append(dep['version'])
            if pyver:
                title += " w/ Python %s" % ' + '.join(sorted(nub(pyver)))

        else:
            raise EasyBuildError("Don't know how to make a PR title for this PR. "
                                 "Please include a title (use --pr-title)")

    full_descr = "(created using `eb --new-pr`)\n"
    if descr is not None:
        full_descr += descr
    # create PR
    pr_target_branch = build_option('pr_target_branch')
    dry_run = build_option('dry_run') or build_option('extended_dry_run')

    msg = '\n'.join([
        '',
        "Opening pull request%s" % ('', " [DRY RUN]")[dry_run],
        "* target: %s/%s:%s" % (pr_target_account, pr_target_repo, pr_target_branch),
        "* from: %s/%s:%s" % (github_account, pr_target_repo, branch),
        "* title: \"%s\"" % title,
        "* labels: %s" % (', '.join(labels) or '(none)'),
        "* description:",
        '"""',
        full_descr,
        '"""',
        "* overview of changes:\n%s" % diff_stat,
        '',
    ])
    print_msg(msg, log=_log, prefix=False)

    if not dry_run:
        g = RestClient(GITHUB_API_URL, username=github_user, token=github_token)
        pulls_url = g.repos[pr_target_account][pr_target_repo].pulls
        body = {
            'base': pr_target_branch,
            'head': '%s:%s' % (github_account, branch),
            'title': title,
            'body': full_descr,
        }
        status, data = pulls_url.post(body=body)
        if not status == HTTP_STATUS_CREATED:
            raise EasyBuildError("Failed to open PR for branch %s; status %s, data: %s", branch, status, data)

        print_msg("Opened pull request: %s" % data['html_url'], log=_log, prefix=False)

        if labels:
            # post labels
            pr = data['html_url'].split('/')[-1]
            pr_url = g.repos[pr_target_account][pr_target_repo].issues[pr]
            try:
                status, data = pr_url.labels.post(body=labels)
                if status == HTTP_STATUS_OK:
                    print_msg("Added labels %s to PR#%s" % (', '.join(labels), pr), log=_log, prefix=False)
            except HTTPError as err:
                _log.info("Failed to add labels to PR# %s: %s." % (pr, err))


def det_account_branch_for_pr(pr_id, github_user=None):
    """Determine account & branch corresponding to pull request with specified id."""

    if github_user is None:
        github_user = build_option('github_user')

    if github_user is None:
        raise EasyBuildError("GitHub username (--github-user) must be specified!")

    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    pr_data, _ = fetch_pr_data(pr_id, pr_target_account, pr_target_repo, github_user)

    # branch that corresponds with PR is supplied in form <account>:<branch_label>
    account = pr_data['head']['label'].split(':')[0]
    branch = ':'.join(pr_data['head']['label'].split(':')[1:])
    github_target = '%s/%s' % (pr_target_account, pr_target_repo)
    print_msg("Determined branch name corresponding to %s PR #%s: %s" % (github_target, pr_id, branch), log=_log)

    return account, branch


@only_if_module_is_available('git', pkgname='GitPython')
def update_pr(pr_id, paths, ecs, commit_msg=None):
    """
    Update specified pull request using specified files

    :param pr_id: ID of pull request to update
    :param paths: paths to categorized lists of files (easyconfigs, files to delete, patches)
    :param ecs: list of parsed easyconfigs, incl. for dependencies (if robot is enabled)
    :param commit_msg: commit message to use
    """

    if commit_msg is None:
        raise EasyBuildError("A meaningful commit message must be specified via --pr-commit-msg when using --update-pr")

    pr_target_account = build_option('pr_target_account')
    pr_target_repo = build_option('pr_target_repo')

    account, branch = det_account_branch_for_pr(pr_id)

    _, _, _, _, diff_stat = _easyconfigs_pr_common(paths, ecs, start_branch=branch, pr_branch=branch,
                                                   start_account=account, commit_msg=commit_msg)

    print_msg("Overview of changes:\n%s\n" % diff_stat, log=_log, prefix=False)

    full_repo = '%s/%s' % (pr_target_account, pr_target_repo)
    msg = "Updated %s PR #%s by pushing to branch %s/%s" % (full_repo, pr_id, account, branch)
    if build_option('dry_run') or build_option('extended_dry_run'):
        msg += " [DRY RUN]"
    print_msg(msg, log=_log, prefix=False)


def check_github():
    """
    Check status of GitHub integration, and report back.
    * check whether GitHub username is available
    * check whether a GitHub token is available, and whether it works
    * check whether git and GitPython are available
    * check whether push access to own GitHub repositories works
    * check whether creating gists works
    * check whether location to local working directories for Git repositories is available (not strictly needed)
    """
    # start by assuming that everything works, individual checks will disable action that won't work
    status = {}
    for action in ['--from-pr', '--new-pr', '--review-pr', '--upload-test-report', '--update-pr']:
        status[action] = True

    print_msg("\nChecking status of GitHub integration...\n", log=_log, prefix=False)

    # check whether we're online; if not, half of the checks are going to fail...
    try:
        print_msg("Making sure we're online...", log=_log, prefix=False, newline=False)
        urlopen(GITHUB_URL, timeout=5)
        print_msg("OK\n", log=_log, prefix=False)
    except URLError as err:
        print_msg("FAIL")
        raise EasyBuildError("checking status of GitHub integration must be done online")

    # GitHub user
    print_msg("* GitHub user...", log=_log, prefix=False, newline=False)
    github_user = build_option('github_user')
    github_account = build_option('github_org') or build_option('github_user')

    if github_user is None:
        check_res = "(none available) => FAIL"
        status['--new-pr'] = status['--update-pr'] = status['--upload-test-report'] = False
    else:
        check_res = "%s => OK" % github_user

    print_msg(check_res, log=_log, prefix=False)

    # check GitHub token
    print_msg("* GitHub token...", log=_log, prefix=False, newline=False)
    github_token = fetch_github_token(github_user)
    if github_token is None:
        check_res = "(no token found) => FAIL"
    else:
        # don't print full token, should be kept secret!
        partial_token = '%s..%s' % (github_token[:3], github_token[-3:])
        token_descr = partial_token + " (len: %d)" % len(github_token)
        if validate_github_token(github_token, github_user):
            check_res = "%s => OK (validated)" % token_descr
        else:
            check_res = "%s => FAIL (validation failed)" % token_descr

    if 'FAIL' in check_res:
        status['--new-pr'] = status['--update-pr'] = status['--upload-test-report'] = False

    print_msg(check_res, log=_log, prefix=False)

    # check git command
    print_msg("* git command...", log=_log, prefix=False, newline=False)
    git_cmd = which('git')
    git_version = get_tool_version('git')
    if git_cmd:
        if git_version in [UNKNOWN, None]:
            check_res = "%s version => FAIL" % git_version
        else:
            check_res = "OK (\"%s\")" % git_version
    else:
        check_res = "(not found) => FAIL"

    if 'FAIL' in check_res:
        status['--new-pr'] = status['--update-pr'] = False

    print_msg(check_res, log=_log, prefix=False)

    # check GitPython module
    print_msg("* GitPython module...", log=_log, prefix=False, newline=False)
    if 'git' in sys.modules:
        git_check = True
        git_attrs = ['GitCommandError', 'Repo']
        for attr in git_attrs:
            git_check &= attr in dir(git)

        if git_check:
            check_res = "OK (GitPython version %s)" % git.__version__
        else:
            check_res = "FAIL (import ok, but module doesn't provide what is expected)"
    else:
        check_res = "FAIL (import failed)"

    if 'FAIL' in check_res:
        status['--new-pr'] = status['--update-pr'] = False

    print_msg(check_res, log=_log, prefix=False)

    # test push access to own GitHub repository: try to clone repo and push a test branch
    msg = "* push access to %s/%s repo @ GitHub..." % (github_account, GITHUB_EASYCONFIGS_REPO)
    print_msg(msg, log=_log, prefix=False, newline=False)
    git_working_dir = tempfile.mkdtemp(prefix='git-working-dir')
    git_repo, res, push_err = None, None, None
    branch_name = 'test_branch_%s' % ''.join(random.choice(ascii_letters) for _ in range(5))
    try:
        git_repo = init_repo(git_working_dir, GITHUB_EASYCONFIGS_REPO, silent=True)
        remote_name = setup_repo(git_repo, github_account, GITHUB_EASYCONFIGS_REPO, 'master',
                                 silent=True, git_only=True)
        git_repo.create_head(branch_name)
        res = getattr(git_repo.remotes, remote_name).push(branch_name)
    except Exception as err:
        _log.warning("Exception when testing push access to %s/%s: %s", github_account, GITHUB_EASYCONFIGS_REPO, err)
        push_err = err

    if res:
        if res[0].flags & res[0].ERROR:
            _log.warning("Error occurred when pushing test branch to GitHub: %s", res[0].summary)
            check_res = "FAIL (error occurred)"
        else:
            check_res = "OK"
    elif github_user:
        if 'git' in sys.modules:
            ver, req_ver = git.__version__, '1.0'
            if LooseVersion(ver) < LooseVersion(req_ver):
                check_res = "FAIL (GitPython version %s is too old, should be version %s or newer)" % (ver, req_ver)
            else:
                check_res = "FAIL (unexpected exception: %s)" % push_err
        else:
            check_res = "FAIL (GitPython is not available)"
    else:
        check_res = "FAIL (no GitHub user specified)"

    if 'FAIL' in check_res:
        status['--new-pr'] = status['--update-pr'] = False

    print_msg(check_res, log=_log, prefix=False)

    # cleanup: delete test branch that was pushed to GitHub
    if git_repo:
        try:
            if git_repo and hasattr(git_repo, 'remotes') and hasattr(git_repo.remotes, 'origin'):
                git_repo.remotes.origin.push(branch_name, delete=True)
        except GitCommandError as err:
            sys.stderr.write("WARNNIG: failed to delete test branch from GitHub: %s\n" % err)

    # test creating a gist
    print_msg("* creating gists...", log=_log, prefix=False, newline=False)
    gist_url = None
    try:
        gist_url = create_gist("This is just a test", 'test.txt', descr='test123', github_user=github_user,
                               github_token=github_token)
        gist_id = gist_url.split('/')[-1]
        _log.info("Gist with ID %s successfully created, now deleting it again...", gist_id)

        delete_gist(gist_id, github_user=github_user, github_token=github_token)
        _log.info("Gist with ID %s deleted!", gist_id)
    except Exception as err:
        _log.warning("Exception occurred when trying to create & delete gist: %s", err)

    if gist_url and re.match('https://gist.github.com/[0-9a-f]+$', gist_url):
        check_res = "OK"
    else:
        check_res = "FAIL (gist_url: %s)" % gist_url
        status['--upload-test-report'] = False

    print_msg(check_res, log=_log, prefix=False)

    # check whether location to local working directories for Git repositories is available (not strictly needed)
    print_msg("* location to Git working dirs... ", log=_log, prefix=False, newline=False)
    git_working_dirs_path = build_option('git_working_dirs_path')
    if git_working_dirs_path:
        check_res = "OK (%s)" % git_working_dirs_path
    else:
        check_res = "not found (suboptimal)"

    print_msg(check_res, log=_log, prefix=False)

    # report back
    if all(status.values()):
        msg = "\nAll checks PASSed!\n"
    else:
        msg = '\n'.join([
            '',
            "One or more checks FAILed, GitHub configuration not fully complete!",
            "See http://easybuild.readthedocs.org/en/latest/Integration_with_GitHub.html#configuration for help.",
            '',
        ])
    print_msg(msg, log=_log, prefix=False)

    print_msg("Status of GitHub integration:", log=_log, prefix=False)
    for action in sorted(status):
        res = ("not supported", 'OK')[status[action]]
        print_msg("* %s: %s" % (action, res), log=_log, prefix=False)
    print_msg('', prefix=False)


def fetch_github_token(user):
    """Fetch GitHub token for specified user from keyring."""

    token, msg = None, None

    if user is None:
        msg = "No GitHub user name provided, required for fetching GitHub token."
    elif not HAVE_KEYRING:
        msg = "Failed to obtain GitHub token from keyring, "
        msg += "required Python module https://pypi.python.org/pypi/keyring is not available."
    else:
        try:
            token = keyring.get_password(KEYRING_GITHUB_TOKEN, user)
        except Exception as err:
            _log.warning("Exception occurred when fetching GitHub token: %s", err)

        if token is None:
            python_cmd = '; '.join([
                "import getpass, keyring",
                "keyring.set_password(\"%s\", \"%s\", getpass.getpass())" % (KEYRING_GITHUB_TOKEN, user),
            ])
            msg = '\n'.join([
                "Failed to obtain GitHub token for %s" % user,
                "Use the following procedure to install a GitHub token in your keyring:",
                "$ python -c '%s'" % python_cmd,
            ])

    if token is None:
        # failed to obtain token, log message explaining why
        _log.warning(msg)
    else:
        _log.info("Successfully obtained GitHub token for user %s from keyring." % user)

    return token


@only_if_module_is_available('keyring')
def install_github_token(github_user, silent=False):
    """
    Install specified GitHub token for specified user.

    :param github_user: GitHub user to install token for
    :param silent: keep quiet (don't print any messages)
    """
    if github_user is None:
        raise EasyBuildError("GitHub user must be specified to install GitHub token")

    # check if there's a token available already
    current_token = fetch_github_token(github_user)

    if current_token:
        current_token = '%s..%s' % (current_token[:3], current_token[-3:])
        if build_option('force'):
            msg = "WARNING: overwriting installed token '%s' for user '%s'..." % (current_token, github_user)
            print_msg(msg, prefix=False, silent=silent)
        else:
            raise EasyBuildError("Installed token '%s' found for user '%s', not overwriting it without --force",
                                 current_token, github_user)

    # get token to install
    token = getpass.getpass(prompt="Token: ").strip()

    # validate token before installing it
    print_msg("Validating token...", prefix=False, silent=silent)
    valid_token = validate_github_token(token, github_user)
    if valid_token:
        print_msg("Token seems to be valid, installing it.", prefix=False, silent=silent)
    else:
        raise EasyBuildError("Token validation failed, not installing it. Please verify your token and try again.")

    # install token
    keyring.set_password(KEYRING_GITHUB_TOKEN, github_user, token)
    print_msg("Token '%s..%s' installed!" % (token[:3], token[-3:]), prefix=False, silent=silent)


def validate_github_token(token, github_user):
    """
    Check GitHub token:
    * see if it conforms expectations (only [a-f]+[0-9] characters, length of 40)
    * see if it can be used for authenticated access
    """
    sha_regex = re.compile('^[0-9a-f]{40}')

    # token should be 40 characters long, and only contain characters in [0-9a-f]
    sanity_check = bool(sha_regex.match(token))
    if sanity_check:
        _log.info("Sanity check on token passed")
    else:
        _log.warning("Sanity check on token failed; token doesn't match pattern '%s'", sha_regex.pattern)

    # try and determine sha of latest commit in easybuilders/easybuild-easyconfigs repo through authenticated access
    sha = None
    try:
        sha = fetch_latest_commit_sha(GITHUB_EASYCONFIGS_REPO, GITHUB_EB_MAIN, github_user=github_user, token=token)
    except Exception as err:
        _log.warning("An exception occurred when trying to use token for authenticated GitHub access: %s", err)

    token_test = bool(sha_regex.match(sha or ''))
    if token_test:
        _log.info("GitHub token can be used for authenticated GitHub access, validation passed")

    return sanity_check and token_test


def find_easybuild_easyconfig(github_user=None):
    """
    Fetches the latest EasyBuild version eb file from GitHub

    :param github_user: name of GitHub user to use when querying GitHub
    """
    dev_repo = download_repo(GITHUB_EASYCONFIGS_REPO, branch='develop', account=GITHUB_EB_MAIN, github_user=github_user)
    eb_parent_path = os.path.join(dev_repo, 'easybuild', 'easyconfigs', 'e', 'EasyBuild')
    files = os.listdir(eb_parent_path)

    # find most recent version
    file_versions = []
    for eb_file in files:
        txt = read_file(os.path.join(eb_parent_path, eb_file))
        for line in txt.split('\n'):
            if re.search(r'^version\s*=', line):
                scope = {}
                exec(line, scope)
                version = scope['version']
                file_versions.append((LooseVersion(version), eb_file))

    if file_versions:
        fn = sorted(file_versions)[-1][1]
    else:
        raise EasyBuildError("Couldn't find any EasyBuild easyconfigs")

    eb_file = os.path.join(eb_parent_path, fn)
    return eb_file


def fetch_pr_data(pr, pr_target_account, pr_target_repo, github_user, full=False, **parameters):
    """Fetch PR data from GitHub"""

    def pr_url(gh):
        """Utility function to fetch data for a specific PR."""
        if pr is None:
            return gh.repos[pr_target_account][pr_target_repo].pulls
        else:
            return gh.repos[pr_target_account][pr_target_repo].pulls[pr]

    status, pr_data = github_api_get_request(pr_url, github_user, **parameters)
    if status != HTTP_STATUS_OK:
        raise EasyBuildError("Failed to get data for PR #%d from %s/%s (status: %d %s)",
                             pr, pr_target_account, pr_target_repo, status, pr_data)

    if full:
        # also fetch status of last commit

        def status_url(gh):
            """Helper function to grab status of latest commit."""
            return gh.repos[pr_target_account][pr_target_repo].commits[pr_data['head']['sha']].status

        status, status_data = github_api_get_request(status_url, github_user, **parameters)
        if status != HTTP_STATUS_OK:
            raise EasyBuildError("Failed to get status of last commit for PR #%d from %s/%s (status: %d %s)",
                                 pr, pr_target_account, pr_target_repo, status, status_data)
        pr_data['status_last_commit'] = status_data['state']

        # also fetch comments
        def comments_url(gh):
            """Helper function to grab comments for this PR."""
            return gh.repos[pr_target_account][pr_target_repo].issues[pr].comments

        status, comments_data = github_api_get_request(comments_url, github_user, **parameters)
        if status != HTTP_STATUS_OK:
            raise EasyBuildError("Failed to get comments for PR #%d from %s/%s (status: %d %s)",
                                 pr, pr_target_account, pr_target_repo, status, comments_data)
        pr_data['issue_comments'] = comments_data

        # also fetch reviews
        def reviews_url(gh):
            """Helper function to grab reviews for this PR"""
            return gh.repos[pr_target_account][pr_target_repo].pulls[pr].reviews

        status, reviews_data = github_api_get_request(reviews_url, github_user, **parameters)
        if status != HTTP_STATUS_OK:
            raise EasyBuildError("Failed to get reviews for PR #%d from %s/%s (status: %d %s)",
                                 pr, pr_target_account, pr_target_repo, status, reviews_data)
        pr_data['reviews'] = reviews_data

    return pr_data, pr_url


def sync_pr_with_develop(pr_id):
    """Sync pull request with specified ID with current develop branch."""
    github_user = build_option('github_user')
    if github_user is None:
        raise EasyBuildError("GitHub user must be specified to use --sync-pr-with-develop")

    target_account = build_option('pr_target_account')
    target_repo = build_option('pr_target_repo')

    pr_account, pr_branch = det_account_branch_for_pr(pr_id)

    # initialize repository
    git_working_dir = tempfile.mkdtemp(prefix='git-working-dir')
    git_repo = init_repo(git_working_dir, target_repo)

    setup_repo(git_repo, pr_account, target_repo, pr_branch)

    # pull in latest version of 'develop' branch from central repository
    msg = "pulling latest version of '%s' branch from %s/%s..." % (target_account, target_repo, GITHUB_DEVELOP_BRANCH)
    print_msg(msg, log=_log)
    easybuilders_remote = create_remote(git_repo, target_account, target_repo, https=True)
    pull_out = git_repo.git.pull(easybuilders_remote.name, GITHUB_DEVELOP_BRANCH)
    _log.debug("Output of 'git pull %s %s': %s", easybuilders_remote.name, GITHUB_DEVELOP_BRANCH, pull_out)

    # create 'develop' branch (with force if one already exists),
    # and check it out to check git log
    git_repo.create_head(GITHUB_DEVELOP_BRANCH, force=True).checkout()
    git_log_develop = git_repo.git.log('-n 3')
    _log.debug("Top of 'git log' for %s branch:\n%s", GITHUB_DEVELOP_BRANCH, git_log_develop)

    # checkout PR branch, and merge develop branch in it (which will create a merge commit)
    print_msg("merging '%s' branch into PR branch '%s'..." % (GITHUB_DEVELOP_BRANCH, pr_branch), log=_log)
    git_repo.git.checkout(pr_branch)
    merge_out = git_repo.git.merge(GITHUB_DEVELOP_BRANCH)
    _log.debug("Output of 'git merge %s':\n%s", GITHUB_DEVELOP_BRANCH, merge_out)

    # check git log, should show merge commit on top
    post_merge_log = git_repo.git.log('-n 3')
    _log.debug("Top of 'git log' after 'git merge %s':\n%s", GITHUB_DEVELOP_BRANCH, post_merge_log)

    # push updated branch back to GitHub (unless we're doing a dry run)
    return push_branch_to_github(git_repo, pr_account, target_repo, pr_branch)
