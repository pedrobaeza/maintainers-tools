#!/usr/bin/python
# -*- coding: utf-8 -*-
#
#
#    OpenERP, Open Source Management Solution
#    This module copyright (C) 2013 Therp BV (<http://therp.nl>).
#               Pedro M. Baeza <pedro.baeza@serviciosbaeza.com>
#
#    Authors Stefan Rijnhart, Holger Brunn
#
#    Inspiration and code snippets taken from the bzr-rewrite plugin
#         copyright (C) 2010-2013 Jelmer Vernooij
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
import os
import sys
import argparse
import time
from os import getcwd, chdir, environ as env
from os.path import isdir
from tempfile import mkstemp
from subprocess import call
from bzrlib.branch import Branch
from bzrlib.builtins import cmd_diff
from bzrlib.config import extract_email_address
from bzrlib.errors import (
    NoSuchRevision,
    UnknownErrorFromSmartServer,
    NoEmailInUsername
)
from bzrlib.option import _parse_revision_str
from bzrlib.tsort import topo_sort

# GitPython
from git import Repo

import logging
from unidecode import unidecode


def detect_project(openupgrade_branch):
    if isdir(openupgrade_branch + '/account_voucher'):
        return 'openupgrade-addons', 'addons/:addons/'
    elif (isdir(openupgrade_branch + '/openerp/tools') or
            isdir(openupgrade_branch + '/bin/tools')):
        return 'openupgrade-server', None
    raise ValueError(
        'Project in %s not recognized' % openupgrade_branch)


def get_abs_path(path):
    if os.path.isabs(path):
        return path
    return getcwd() + os.sep + path


def replay_missing(openupgrade_branch, upstream, git_repo_dir,
                   branch, interactive=False):
    """
    Replay differences between an openupgrade branch and the correspoding
    upstream branch, committing each revision in the openupgrade's history as
    a cherry picking merge to the git repo branch.
    """

    def find_difference(source, target):
        source.lock_write()
        try:
            """ Get missing revision in two branches """
            source_revision = source.last_revision()
            target_revision = target.last_revision()
            source.repository.fetch(target.repository, target_revision)
            repo_graph = source.repository.get_graph()
            logging.debug("Searching for missing revisions between %s and %s" %
                          (source.base, target.base))
            result = repo_graph.find_difference(
                source_revision, target_revision)
        finally:
            source.unlock()
        return result

    git_repo = Repo(git_repo_dir)

    project, prefix = detect_project(openupgrade_branch)
    ou_branch = Branch.open_containing(openupgrade_branch)[0]
    logging.debug('finding differences')

    _, todo_set = find_difference(upstream, ou_branch)
    logging.debug("%s revisions not in the upstream branch", len(todo_set))

    try:
        ou_branch.lock_write()
        parent_map = ou_branch.repository.get_graph().get_parent_map(todo_set)
    finally:
        ou_branch.unlock()
    ordered_set = topo_sort(parent_map)
    logging.debug("%s revisions not in the local tree", len(ordered_set))
    # Replay the remaining revisions

    cwd = getcwd()
    FNULL = open(os.devnull, 'w')

    for revid in ordered_set:
        # Retrieve the revision id if the revision occurs in the
        # branch history. Launchpad used to return NoSuchRevision
        # in the past but an unknown error now.
        revno = '...'
        oldrev = upstream.repository.get_revision(revid)
        try:
            revno = ou_branch.revision_id_to_revno(revid)
        except NoSuchRevision:
            continue
        except UnknownErrorFromSmartServer, e:
            # TODO: check for other 'unknown errors' than just
            # no revision_id found
            logging.debug('UnknownErrorFromSmartServer: %s', e)
            continue

        revisionspec = _parse_revision_str(
            "%s..%s" % (int(revno) - 1, int(revno)))

        # Get bug references from the merged bzr revisions
        bugs = []
        ou_branch.lock_write()
        delta = ou_branch.get_revision_delta(revno)
        for rev_spec in ou_branch.iter_merge_sorted_revisions(
                stop_revision_id=ou_branch.get_rev_id(int(revno) - 1),
                start_revision_id=oldrev.revision_id):
            rev = ou_branch.repository.get_revision(rev_spec[0])
            for bug in rev.iter_bugs():
                if bug[0] not in bugs:
                    bugs.append(bug[0])
        ou_branch.unlock()

        print "\nCommitting http://bazaar.launchpad.net/" \
              "~openupgrade-committers/%s/%s/revision/%s" % (
              project, branch, revno)
        print oldrev.message
        if interactive:
            question = raw_input("Do you want to apply it? (Y/n) ")
            if question.upper() == 'N':
                continue
        # Catch bzr diff by swapping out sys.stdout
        stdout = sys.stdout

        tmpfile = mkstemp()[1]

        chdir(openupgrade_branch)
        with open(tmpfile, 'wb') as diff_out:
            sys.stdout = diff_out
            cmd_diff().run(revisionspec, prefix=prefix)
            sys.stdout = stdout
            diff_out.close()
        chdir(git_repo_dir)

        with open(tmpfile, 'rb') as diff_out:
            diff_out.seek(0)
            tmpfile2 = mkstemp()[1]
            with open(tmpfile2, 'wb') as patch_out:
                patch = call(
                    ['patch', '-p', '0', '-f'],
                    stdin=diff_out, stdout=FNULL, stderr=patch_out)
                patch_out.close()

        # Handle renamings
        renaming_incorrect = False
        for renaming in delta.renamed:
            if call(['git', 'mv', renaming[0], renaming[1]], stdout=FNULL):
                renaming_incorrect = True

        if patch or renaming_incorrect:  # patch or renaming failed
            if not interactive:
                logging.error("Patch or renaming failed, reverting")
                # Undo renaming
                for renaming in delta.renamed:
                    call(['git', 'mv', renaming[1], renaming[0]], stdout=FNULL)
                with open(tmpfile2, 'rb') as patch_out:
                    patch_out.close()

                # git reset --hard
                git_repo.head.reset(working_tree=True)
                # Not in GitPython API
                call(['git', 'clean', '-f'], stdout=FNULL)
                continue
            else:
                raw_input("Patch or renaming failed. Solve it manually and "
                          "then press Enter to continue...")

        # Split up name and email
        author = oldrev.get_apparent_authors()[0]
        try:
            author_email = extract_email_address(author)
            if author.find(author_email) > 2:
                author_name = author[:author.find(author_email) - 2]
            else:
                author_name = author
        except NoEmailInUsername:
            author_email = 'no_email@example.org'
            author_name = author

        message = oldrev.message
        if bugs:
            message += '\n%s' % '\n'.join(set(bugs))

        # index.add() ignores 'force' parameter and adds .git system dir
        call(['git', 'add', '--all'])
        # Commit takes the author info from the environment
        env['GIT_AUTHOR_NAME'] = unidecode(author_name)
        env['GIT_AUTHOR_EMAIL'] = author_email
        env['GIT_AUTHOR_DATE'] = time.strftime(
            '%Y-%m-%d %H:%M:%S', time.gmtime(oldrev.timestamp))
        index = git_repo.index
        index.commit(message)
        continue
    chdir(cwd)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-l', '--log_level',
        help='Log level (default is \'INFO\')')
    parser.add_argument(
        '-i', '--interactive', action='store_true', default=False,
        help="This option pauses each failed replay to allow to resolve "
             "conflicts manually before continuing")
    parser.add_argument(
        '-b', '--branch', default="7.0", help="Branch used for references")
    parser.add_argument(
        'openupgrade_branch',
        help='OpenUpgrade project branch. Should be a local branch')
    parser.add_argument(
        'upstream_branch',
        help=('Upstream Odoo branch to determine which revisions '
              'are missing. This needs to be a local, writable branch.'))
    parser.add_argument('git_repo_dir')

    arguments = parser.parse_args(argv)

    if arguments.log_level:
        logging.getLogger().setLevel(getattr(logging, arguments.log_level))

    interactive = arguments.interactive
    branch = arguments.branch

    if not isdir(arguments.git_repo_dir):
        sys.exit("%s is not a directory\n" % arguments.git_repo_dir)

    upstream = Branch.open(arguments.upstream_branch)

    if not isdir(arguments.openupgrade_branch):
        sys.exit("%s is not a directory\n" % arguments.openupgrade_branch)

    replay_missing(
        get_abs_path(arguments.openupgrade_branch), upstream,
        get_abs_path(arguments.git_repo_dir), branch, interactive)

if __name__ == "__main__":
    logging.basicConfig(
        format='%(levelname)s: %(message)s', level=logging.INFO)
    sys.exit(main(sys.argv[1:]))
