import traceback
from . import pg_advisory_lock
from odoo import registry
import arrow
from pathlib import Path
import tempfile
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.logsio_writer import LogsIOWriter
from odoo.addons.queue_job.exception import (
    RetryableJobError,
    JobError,
)
import logging
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from contextlib import contextmanager, closing

logger = logging.getLogger(__name__)


class InvalidBranchName(Exception):
    pass


class NewBranch(Exception):
    pass

class MergeConflict(Exception):
    def __init__(self, conflicts):
        self.conflicts = conflicts

class Repository(models.Model):
    _name = 'cicd.git.repo'

    short = fields.Char(
        compute="_compute_shortname", string="Name", compute_sudo=True)
    webhook_id = fields.Char("Webhook ID", help="/trigger/repo/<this id>")
    webhook_secret = fields.Char("Webhook Secret")
    machine_id = fields.Many2one(
        'cicd.machine', string="Development Machine",
        required=True, domain=[('ttype', '=', 'dev')])
    name = fields.Char("URL", required=True)
    login_type = fields.Selection([
        ('username', 'Username'),
        ('key', 'Key'),
    ])
    analyze_last_n_commits = fields.Integer(
        "Anlayze last n commits", default=200)
    key = fields.Text("Key")
    username = fields.Char("Username")
    password = fields.Char("Password")
    skip_paths = fields.Char("Skip Paths", help="Comma separated list")
    branch_ids = fields.One2many(
        'cicd.git.branch', 'repo_id', string="Branches")
    url = fields.Char(compute="_compute_url", compute_sudo=True)
    default_branch = fields.Char(default="master", required=True)
    ticketsystem_id = fields.Many2one(
        'cicd.ticketsystem', string="Ticket System")
    release_ids = fields.One2many(
        'cicd.release', 'repo_id', string="Releases")
    default_simulate_install_id_dump_id = fields.Many2one(
        'cicd.dump', string="Default Simluate Install Dump")
    never_cleanup = fields.Boolean("Never Cleanup")
    cleanup_untouched = fields.Integer(
        "Cleanup after days", default=20, required=True)
    autofetch = fields.Boolean("Autofetch", default=True)
    garbage_collect = fields.Boolean(
        "Garbage Collect to reduce size", default=True)
    initialize_new_branches = fields.Boolean("Initialize new Branches")
    release_tag_prefix = fields.Char(
        "Release Tag Prefix", default="release-", required=True)
    remove_web_assets_after_restore = fields.Boolean(
        "Remove Webassets", default=True)
    ttype = fields.Selection([
        ('gitlab', 'GitLab'),
        ('bitbucket', 'BitBucket'),
        ('github', 'GitHub'),
    ], string="Type")

    make_dev_dumps = fields.Boolean("Make Dev Dumps")
    ticketsystem_id = fields.Many2one(
        "cicd.ticketsystem", string="Ticket-System")

    _sql_constraints = [
        ('name_unique', "unique(name)", _("Only one unique entry allowed.")),
    ]

    def _get_repo_path(self, machine):
        from . import MAIN_FOLDER_NAME
        self.ensure_one()
        path = Path(machine.workspace) / (MAIN_FOLDER_NAME + "_" + self.short)
        return path

    def _get_lockname(self, machine=None, path=None):
        self.ensure_one()
        machine = machine or self.machine_id
        path = path or self._get_repo_path(machine)
        return f"repo_{machine.id}_{path}"

    @api.constrains("username")
    def _check_username(self):
        for rec in self:
            if rec.username and '@' in rec.username:
                raise ValidationError(
                    _("Please use the login username instead of email address"))

    def _compute_shortname(self):
        for rec in self:
            rec.short = rec.name.split("/")[-1]

    @api.depends('username', 'password', 'name')
    def _compute_url(self):
        for rec in self:
            if rec.login_type == 'username':
                url = ""
                for prefix in [
                    'https://',
                    'http://',
                    'ssh://',
                    'ssh+git://'
                ]:
                    if rec.name.startswith(prefix):
                        url = \
                            f'{prefix}{rec.username}:{rec.password}@{rec.name[len(prefix):]}'
                        break
                rec.url = url
            else:
                rec.url = rec.name

    def _get_zipped(self, logsio, commit):
        machine = self.machine_id
        with self._temp_repo(machine=machine) as repo_path:
            filename = Path(tempfile.mktemp(suffix='.get_zipped'))
            try:
                with machine._shell(repo_path, logsio=logsio) as shell:
                    try:
                        shell.checkout_commit(commit)
                        shell.X(["git", "clean", "-xdff"])
                        shell.X(["tar", "cfz", filename, "-C", repo_path, '.'])
                        content = shell.get(filename)
                        shell.X(["rm", filename])
                        return content
                    finally:
                        shell.rm(repo_path)

            finally:
                if filename.exists():
                    filename.unlink()

    def _technical_clone_repo(
            self, path, machine, logsio=None, branch=None, depth=None):

        with machine._gitshell(
            self, cwd=self.machine_id.workspace, logsio=logsio
        ) as shell:
            temppath = tempfile.mktemp(suffix='.clone_repo')

            cmd = ["git", "clone"]
            if branch:
                cmd += ["--branch", branch]
            if depth:
                cmd += ["--depth", str(depth)]
            cmd += [self.url, temppath]
            shell.X(cmd)

            if shell.exists(path):
                # clone may happened during that clone
                shell.remove(temppath)
            else:
                shell.X(["mv", temppath, path])
            return

    @contextmanager
    def _temp_repo(
            self, machine, logsio=None, branch=None, depth=None,
            pull=False):

        path = tempfile.mktemp(suffix='.temporary_repo')

        self._technical_clone_repo(
            path, machine, branch=branch, depth=depth)
        try:
            yield path

        finally:
            with machine._shell() as shell:
                shell.remove(path)

    def _get_main_repo(
            self, destination_folder=False,
            logsio=None, machine=None, limit_branch=None,
            depth=None
            ):

        self.ensure_one()
        machine = machine or self.machine_id
        if not machine.workspace:
            raise ValidationError(_("Please configure a workspace!"))
        path = None

        repo_path = self._get_repo_path(machine)
        self.clone_repo(machine, repo_path, logsio)

        path = destination_folder or repo_path
        if path != repo_path:
            with machine._shell(
                    cwd=self.machine_id.workspace, logsio=logsio) as shell:
                if not self._is_healthy_repository(shell, path):
                    shell.rm(path)

                    if limit_branch:
                        # make sure branch exists in source repo
                        with shell.clone(cwd=repo_path) as shell2:
                            shell2.checkout_branch(limit_branch)

                    cmd = ["git", "clone"]
                    if limit_branch:
                        cmd += ["--branch", limit_branch]
                    cmd += [repo_path, path]
                    shell.X(cmd)
        return path

    def _get_remotes(self, shell):
        remotes = shell.X(["git", "remote", "-v"])['stdout'].strip().split("\n")
        remotes = list(filter(bool, [x.split("\t")[0] for x in remotes]))
        return list(set(remotes))

    @api.model
    def _clear_branch_name(self, branch):
        branch = branch.strip()

        if " (" in branch:
            branch = branch.split(" (")[0]

        if "->" in branch:
            branch = branch.split("->")[-1].strip()

        if "* " in branch:
            branch = branch.replace("* ", "")
        branch = branch.strip()

        if any(x in branch for x in "():?*/\\!\"\'"):
            raise InvalidBranchName(branch)
        return branch

    def fetch(self):
        self._cron_fetch()

    @api.model
    def _cron_fetch(self):
        breakpoint()
        repos = self
        if not repos:
            repos = self.search([('autofetch', '=', True)])

        for repo in repos:
            repo.with_delay(
                identity_key=f"queuejob_fetch_{repo.short}")._queuejob_fetch()

    def _queuejob_fetch(self):
        self.ensure_one()
        try:
            if not self.login_type:
                raise ValidationError(f"Login-Type missing for {self.name}")
            with LogsIOWriter.GET(self.name, 'fetch') as logsio:
                breakpoint()
                repo_path = self._get_main_repo(logsio=logsio)

                with self.machine_id._gitshell(
                        repo=self, cwd=repo_path, logsio=logsio) as shell:
                    updated_branches = set()

                    for remote in self._get_remotes(shell):

                        fetch_output = shell.X([
                            "git", "fetch", remote, '--dry-run'])[
                                'stderr'].strip().split("\n")

                        fetch_info = \
                            list(filter(lambda x: " -> " in x, fetch_output))

                        for fi in fetch_info:
                            while "  " in fi:
                                fi = fi.replace("  ", " ")
                            fi = fi.strip()
                            if '[new tag]' in fi:
                                continue
                            elif '[new branch]' in fi:
                                branch = fi.replace(
                                    "[new branch]", "").split("->")[0].strip()
                            else:
                                branch = fi.split("/")[-1]
                            try:
                                branch = self._clear_branch_name(branch)
                            except InvalidBranchName:
                                logsio.error("Invalid Branch name: {branch}")
                                continue

                            if not branch.startswith(self.release_tag_prefix):
                                updated_branches.add(branch)

                        del fetch_info

                    if not updated_branches:
                        return

                    for branch in set(updated_branches):
                        self._fetch_branch(branch)
                        del branch

        except Exception:
            msg = traceback.format_exc()
            if logsio:
                logsio.error(msg)
            logger.error('error', exc_info=True)
            raise

    def _fetch_branch(self, branch):
        assert isinstance(branch, str)
        self.ensure_one()
        self.with_delay(
            identity_key=(
                "fetch_updated_branch_"
                f"{self.short or self.name}_branch:{branch}"
                ),

        )._cron_fetch_update_branches({
            'updated_branches': [branch],
        })

    def _clean_remote_branches(self, branches):
        """
        origin/pre_master1']  --> pre_master1
        """
        for branch in branches:
            if '->' in branch:
                continue
            yield branch.split("/")[-1].strip()

    def _checkout_branch_recreate_repo_on_need(self, shell, branch):
        try:
            shell.checkout_branch(branch)
        except Exception as ex:
            breakpoint()
            shell.logsio.error(ex)

            severity = self._exception_meaning(ex)
            if severity == 'broken':
                shell.logsio.info("Recreating workspace folder")
                shell.rm(shell.cwd)
                self.clone_repo(shell.machine, shell.cwd, shell.logsio)
                shell.checkout_branch(branch)
            else:
                raise

    def _pull_hard_reset(self, shell, branch):
        """
        * avoid fast forward git extra commits
        * deletes local existance of branch

        """
        shell.X([
            "git", "checkout", self.default_branch,
            "-f"])
        if shell.branch_exists(branch):
            shell.X([
                "git", "branch", "-D", branch],
                allow_error=True)
        shell.X(["git", "checkout", branch])
        shell.X([
            "git", "reset", "--hard",
            f"origin/{branch}"])

        # remove existing instance folder to refetch
        db_branch = self.env['cicd.git.branch'].search([
            ('name', '=', branch),
            ('repo_id', '=', self.id)
        ])
        shell.rm(db_branch._get_instance_folder(
            shell.machine))

    def _pull(self, shell, branch):
        # option P makes .git --> .git/
        shell.X([
            "ls -pA |grep -v \\.git\\/ |xargs rm -Rf"])
        shell.X(["git", "pull"])
        shell.X(["git", "checkout", "-f"])

    def _prepare_pulled_branch(self, shell, branch):
        releases = self.env['cicd.release'].search([
            ('repo_id', '=', self.id)])
        candidate_branch_names = releases.item_ids.mapped('item_branch_name')
        try:
            breakpoint()
            logsio = shell.logsio
            logsio.info(f"Pulling {branch}...")
            shell.X(["git", "fetch", "origin", branch])
            logsio.info(f"pulled {branch}...")

            self._checkout_branch_recreate_repo_on_need(
                shell, branch)

            if branch in candidate_branch_names:
                self._pull_hard_reset(shell, branch)
            else:
                self._pull(shell, branch)
            shell.X([
                "git", "submodule", "update",
                "--init", "--recursive"])

        except Exception as ex:
            logger.error('error', exc_info=True)
            if self._exception_meaning(ex) == 'retry':
                raise RetryableJobError(
                    str(ex), seconds=10,
                    ignore_retry=True) from ex
            raise

    def _cron_fetch_update_branches(self, data):
        repo = self
        # checkout latest / pull latest
        updated_branches = data['updated_branches']
        breakpoint()

        with LogsIOWriter.GET(repo.name, 'fetch') as logsio:
            repo_path = repo._get_main_repo(logsio=logsio)
            repo = repo.with_context(active_test=False)
            machine = repo.machine_id

            with pg_advisory_lock(self.env.cr, repo._get_lockname(
                    machine, repo_path),
                    detailinfo=(
                        f"cron_fetch_updated_branches {updated_branches}"
                )):

                with repo.machine_id._gitshell(
                        repo, cwd=repo_path, logsio=logsio) as shell:

                    for branch in updated_branches:
                        repo._prepare_pulled_branch(shell, branch)

                    # if completely new then all branches:
                    if not repo.branch_ids:
                        for branch in shell.X(["git", "branch"])[
                                'stdout'].strip().split("\n"):
                            branch = self._clear_branch_name(branch)
                            updated_branches.append(branch)

                    for branch in updated_branches:
                        shell.checkout_branch(branch, cwd=repo_path)
                        name = branch
                        del branch

                        date_registered = arrow.utcnow().strftime(DTF)
                        if not (branch := repo.branch_ids.filtered(
                            lambda x: x.name == name)):
                            branch = repo.branch_ids.create({
                                'name': name,
                                'date_registered': date_registered,
                                'repo_id': repo.id,
                            })
                            branch._checkout_latest(
                                shell, logsio=logsio, machine=machine)
                            branch._update_git_commits(
                                shell, logsio, force_instance_folder=repo_path)

                        shell.checkout_branch(
                            repo.default_branch, cwd=repo_path)
                        del name

                    if not repo.branch_ids and not updated_branches:
                        if repo.default_branch:
                            updated_branches.append(repo.default_branch)

                    for branch_name in updated_branches:
                        self._postprocess_branch_updates(
                            shell, repo, repo_path, branch_name, logsio=logsio)

    def _postprocess_branch_updates(self, shell, repo, repo_path, branch_name, logsio):
        """
        If a branch was updated, then the
        """
        if not branch_name:
            return

        assert isinstance(branch_name, str)

        # for contains_commit function;
        # clear caches tested in shell and removes
        # all caches; method_name
        repo.clear_caches()
        branch = repo.branch_ids.filtered(
            lambda x: x.name == branch_name)
        machine = repo.machine_id
        branch._checkout_latest(
            shell, logsio=logsio, machine=machine)
        branch._update_git_commits(shell, logsio)
        branch._compute_latest_commit(shell)
        branch._compute_state()
        branch._trigger_rebuild_after_fetch()
        shell.checkout_branch(
            repo.default_branch, cwd=repo_path)

    def _is_healthy_repository(self, shell, path):
        self.ensure_one()
        healthy = False
        if shell.exists(path):
            try:
                rc = shell.X([
                    "git", "status", "-s"], cwd=path, logoutput=False,
                    allow_error=True
                )
                if rc['exit_code']:
                    if '.git/index.lock' in rc['std_err']:
                        pass
                    else:
                        raise
                healthy = True

            except Exception:
                healthy = False
        return healthy

    def clone_repo(self, machine, path, logsio):
        with machine._gitshell(self, cwd="", logsio=logsio) as shell:
            if not self._is_healthy_repository(shell, path):
                with pg_advisory_lock(
                        self.env.cr,
                        self._get_lockname(machine, path), 'clone_repo'
                ):
                    shell.rm(path)
                    self._technical_clone_repo(path, machine=machine)

    def _merge_commits_on_target(self, shell, target, commits):
        conflicts = []
        for commit in commits:
            # we use git functions to retrieve deltas, git sorting and
            # so; we want to rely on stand behaviour git.
            shell.checkout_branch(target)
            history = []
            try:
                shell.X(["git", "merge", commit.name])
                history.append(commit.name)
            except Exception as ex:
                conflicts.append(commit)
        return conflicts

    def _recreate_branch_from_commits(
        self, source_branch, commits,
        target_branch_name, logsio, make_info_commit_msg,
    ):
        """
        Creates a new branch with given commit ids based on source.
        Deletes existing target_branch_name.

        If merge conflicts exist, then all not mergable commits are returned.

        """
        if not commits:
            return
        self.ensure_one()

        # we use a working repo
        assert target_branch_name
        assert commits._name == 'cicd.git.commit'
        machine = self.machine_id

        with self._temp_repo(machine=self.machine_id) as repo_path:
            self = self.with_context(active_test=False)
            message_commit = None  # commit sha of the created message commit
            with machine._gitshell(
                self, cwd=repo_path, logsio=logsio
            ) as shell:

                # clear the current candidate
                shell.checkout_branch(source_branch)
                if shell.branch_exists(target_branch_name):
                    shell.X(["git", "branch", "-D", target_branch_name])
                logsio.info("Making target branch {target_branch.name}")
                shell.X([
                    "git", "checkout", "--no-guess",
                    "-b", target_branch_name])

                conflicts = self._merge_commits_on_target(
                    shell, target_branch_name, commits)
                if conflicts:
                    raise MergeConflict(conflicts)

                # pushes to mainrepo locally not to web because its
                # cloned to temp directory
                shell.X(["git", "remote", "set-url", 'origin', self.url])

                message_commit_sha = None
                if make_info_commit_msg:
                    shell.X([
                        "git", "commit", "--allow-empty", "-m",
                        make_info_commit_msg])
                    message_commit_sha = shell.X([
                        "git", "log", "-n1", "--format=%H"])['stdout'].strip()

                shell.X([
                    "git", "push", "--set-upstream", "-f", 'origin',
                    target_branch_name])

                if not (target_branch := self.branch_ids.filtered(
                        lambda x: x.name == target_branch_name)):
                    target_branch = self.branch_ids.create({
                        'repo_id': self.id,
                        'name': target_branch_name,
                    })
                if not target_branch.active:
                    target_branch.active = True
                target_branch._update_git_commits(
                    shell, logsio, force_instance_folder=repo_path)
                target_branch._compute_latest_commit(shell)
                if message_commit_sha:
                    message_commit = target_branch.commit_ids.filtered(
                        lambda x: x.name == message_commit_sha)
                    message_commit.ensure_one()

        return message_commit

    def _merge(self, source, dest, set_tags, logsio=None):
        assert source._name in ['cicd.git.branch', 'cicd.git.commit']
        assert dest._name == 'cicd.git.branch'
        source.ensure_one()
        dest.ensure_one()

        machine = self.machine_id
        with self._temp_repo(machine=machine) as repo_path:
            with machine._gitshell(
                self, cwd=repo_path, logsio=logsio
            ) as shell:

                shell.checkout_branch(dest.name)
                commitid = shell.X([
                    "git", "log", "-n1", "--format=%H"])['stdout'].strip()
                if source._name == 'cicd.git.branch':
                    branches = [self._clear_branch_name(x) for x in shell.X([
                        "git", "branch", "--contains", commitid])[
                            'stdout'].strip().split("\n")]
                    if source.name in branches:
                        return False
                breakpoint()
                shell.checkout_branch(source.name)
                shell.checkout_branch(dest.name)
                count_lines = len(shell.X(["git", "diff", "-p", source.name])[
                    'stdout'].strip().split("\n"))
                shell.X(["git", "merge", source.name])
                for tag in set_tags:
                    shell.X(["git", "tag", '-f', tag.replace(':', '_').replace(
                        ' ', '_')])
                shell.X(["git", "remote", "set-url", 'origin', self.url])
                shell.X(["git", "push", '--tags'])
                shell.X(["git", "push"])
                mergecommitid = shell.X([
                    "git", "log", "-n1", "--format=%H"])['stdout'].strip()

                return count_lines, mergecommitid

    @api.model
    def _cron_cleanup(self):
        FT = "%Y-%m-%d %H:%M:%S"
        for repo in self.search([
            ('never_cleanup', '=', False),
        ]):
            dt = arrow.get().shift(
                days=-1 * repo.cleanup_untouched).strftime(FT)

            # try nicht unbedingt notwendig; bei __exit__
            # wird ein close aufgerufen
            db_registry = registry(self.env.cr.dbname)
            branches = repo.branch_ids.filtered(
                lambda x: x.last_access or x.date_registered).filtered(
                lambda x: (
                    x.last_access or x.date_registered).strftime(FT) < dt)

            # keep release branches
            releases = self.env['cicd.release'].search([
                ('repo_id', '=', repo.id)])
            names = list(releases.mapped('branch_id.name'))
            names += list(releases.mapped('candidate_branch'))
            branches = branches.filtered(lambda x: x.name not in names)
            del names

            # keep new, dev and done
            branches = branches.filtered(lambda x: x.state in [
                'new', 'dev', 'done'])

            # keep branches with recents updates
            def outdated_commits(branch):
                if not branch.latest_commit_id:
                    return True
                if not branch.latest_commit_id.date:
                    return True
                return bool(branch.latest_commit_id.date.strftime(FT) < dt)

            branches = branches.filtered(outdated_commits)

            with closing(db_registry.cursor()) as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                for branch in branches.with_env(env):
                    branch.active = False
            del dt

    def new_branch(self):
        return {
            'view_type': 'form',
            'res_model': 'cicd.git.branch.new',
            'context': {
                'default_repo_id': self.id,
            },
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'flags': {'form': {
                'action_buttons': False,
                'initial_mode': 'edit',
                # 'footer_to_buttons': False,
                # 'not_interactiable_on_create': False,
                # 'disable_autofocus': False,
                # 'headless': False,  9.0 and others?
            }},
            'options': {
                # needs module web_extended_actions
                'hide_breadcrumb': True,
                'replace_breadcrumb': True,
                'clear_breadcrumbs': True,
            },
            'target': 'new',
        }

    def _get_base_url(self):
        self.ensure_one()
        url = self.url
        if not url.endswith("/"):
            url += '/'
        if url.startswith("ssh://git@"):
            url = url.replace("ssh://git@", "https://")
        return url

    def _get_url(self, ttype, object, object2=None):
        self.ensure_one()
        if self.ttype == 'gitlab':
            if ttype == 'commit':
                return self._get_base_url() + "-/commit/" + object.name
            elif ttype == 'compare':
                return self._get_base_url() + "-/compare?from=" + object + \
                    "&to=" + object2
            else:
                raise NotImplementedError()
        elif self.ttype == 'bitbucket':
            if ttype == 'commit':
                return self._get_base_url() + "/commits/" + object.name
            else:
                raise NotImplementedError()
        else:
            raise NotImplementedError()

    def _exception_meaning(self, ex):
        """
        Checks error message from git if the repo is broken and
        needs recreation.  Recreation is a problem, because current state
        is lost, so this should be avoided if possible.
        """

        exmessage = str(ex)
        if any(x in exmessage for x in [
            '.git/index.lock',
        ]):
            return 'retry'
        if any(x in exmessage for x in [
            'could not read Username for',
        ]):
            return 'hangs_not_broken'
        return 'broken'

    def purge_old_sources(self):
        for rec in self:
            for branch in self.env['cicd.git.branch'].search([
                ('repo_id', '=', rec.id),
                ('active', '=', False)
            ]):
                branch.with_delay().purge_instance_folder()