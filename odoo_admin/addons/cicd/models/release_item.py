import traceback
from . import pg_advisory_lock
import psycopg2
import arrow
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError
from ..tools.logsio_writer import LogsIOWriter
import logging
logger = logging.getLogger(__name__)


class ReleaseItem(models.Model):
    _inherit = ['mail.thread', 'cicd.open.window.mixin']
    _name = 'cicd.release.item'
    _order = 'id desc'
    _log_access = False

    name = fields.Char("Version")
    branch_ids = fields.One2many('cicd.release.item.branch', 'item_id', tracking=True)
    release_id = fields.Many2one('cicd.release', string="Release", required=True, ondelete="cascade")
    planned_date = fields.Datetime("Planned Deploy Date", default=lambda self: fields.Datetime.now(), tracking=True)
    done_date = fields.Datetime("Done", tracking=True)
    changed_lines = fields.Integer("Changed Lines", tracking=True)
    final_curtain = fields.Datetime("Final Curtains", tracking=True)
    log_release = fields.Text("Log", readonly=True)
    state = fields.Selection([
        ("collecting", "Collecting"),
        ('collecting_merge_conflict', 'Collecting Merge Conflict'),
        ('integrating', 'Integration'),
        ('failed_merge', 'Failed: Merge Conflict'),
        ('failed_integration', 'Failed: Integration'),
        ('failed_technically', 'Failed technically'),
        ('failed_too_late', 'Failed: too late'),
        ('ready', 'Ready'),
        ('done', 'Done'),
    ], string="State", default='new', required=True, tracking=True)
    computed_summary = fields.Text("Computed Summary", compute="_compute_summary", tracking=True)
    count_failed_queuejobs = fields.Integer("Failed Jobs", compute="_compute_failed_jobs")
    try_counter = fields.Integer("Try Counter", tracking=True)
    commit_id = fields.Many2one('cicd.git.commit', string="Released commit", help="After merging all tested commits this is the commit that holds all merged commits.")

    release_type = fields.Selection([
        ('standard', 'Standard'),
        ('hotfix', 'Hotfix'),
    ], default="standard", required=True, readonly=True)

    @api.constrains("state")
    def _ensure_one_item_only(self):
        for rec in self:
            if rec.state in ['new']:
                if rec.release_id.item_ids.filtered(lambda x: x.release_type == 'standard' and x.id != rec.id and x.state in ['new']):
                    breakpoint()
                    raise ValidationError(_("There may only be one new standard item!"))

    def _on_done(self):
        # if not self.changed_lines:
        #     msg = "Nothing new to deploy"
        self.done_date = fields.Datetime.now()
        self.release_id.message_post_with_view(
            self.env.ref('cicd.mail_release_done'),
            values={'summary': self.computed_summary}
        )
        self.state = 'done'
        self.branch_ids._compute_state()

    def _compute_failed_jobs(self):
        for rec in self:
            jobs = self.env['queue.job'].search([
                ('identity_key', 'ilike', f'release-item {rec.id}')
            ])
            rec.count_failed_queuejobs = len(jobs.filtered(lambda x: x.state == 'failed'))

    @api.model
    def create(self, vals):
        release = self.env['cicd.release'].browse(vals['release_id'])
        vals['name'] = release.sequence_id.next_by_id()
        res = super().create(vals)
        return res

    def _compute_summary(self):
        for rec in self:
            summary = []
            for branch in rec.branch_ids.sorted(lambda x: x.date):
                summary.append(f"* {branch.enduser_summary or branch.name}")
            rec.computed_summary = '\n'.join(summary)

    def _trigger_do_release(self):
        for rec in self:
            rec.with_delay(
                identity_key=f"release-item {rec.id}",
            )._do_release()

    def perform_release(self):
        self._do_release()

    def redo(self):
        self.ensure_one()
        self.with_context(override_release_state=True)._do_release()

    def _do_release(self):
        breakpoint()
        logsio = None
        try:
            self.env.cr.execute("select id from cicd_release where id=%s for update nowait", (self.release_id.id,))
        except psycopg2.errors.LockNotAvailable as ex:
            raise RetryableJobError(
                f"Could not work exclusivley on release {self.release_id.id} - retrying in few seconds",
                ignore_retry=True, seconds=15) from ex
        if not self.release_id.active:
            return
        if self.planned_date > fields.Datetime.now():
            return

        try:
            if self.state not in ['new']:
                if not self.env.context.get("override_release_state"):
                    return
            if self.release_type == 'hotfix' and not self.branch_ids:
                raise ValidationError("Hotfix requires explicit branches.")

            if not self.branch_ids:
                # wait for releases
                return

            if not self.commit_id:  # needs a collected commit with everything on it
                raise RetryableJobError(
                    "Missing commit",
                    ignore_retry=True, seconds=120)

            if self.commit_id.test_state != 'success':
                self.log_release = f"Release is missing a valid test run of {self.commit_id.name}"
                return

            with self.release_id._get_logsio() as logsio:

                self.try_counter += 1
                release = self.release_id
                repo = self.release_id.repo_id.with_context(active_test=False)
                with pg_advisory_lock(self.env.cr, repo._get_lockname(), detailinfo=f"release_merge_new_branch {release.name}"):
                    candidate_branch = repo.branch_ids.filtered(lambda x: x.name == self.release_id.candidate_branch)
                    candidate_branch.ensure_one()
                    if not candidate_branch.active:
                        raise UserError(f"Candidate branch '{self.release_id.candidate_branch}' is not active!")
                    changed_lines, merge_commit_id = repo._merge(
                        self.commit_id,
                        release.branch_id,
                        set_tags=[f'{repo.release_tag_prefix}{self.name}-' + fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                        logsio=logsio,
                    )
                    self.changed_lines += changed_lines
                    self.env.cr.commit()

                errors = self.release_id._technically_do_release(self, merge_commit_id)
                if errors:
                    breakpoint()
                    raise Exception(','.join(map(str, filter(bool, errors))))

                if logsio:
                    self.log_release = ','.join(logsio.get_lines())
                self._on_done()
                self.env.cr.commit()

        except RetryableJobError:
            raise

        except Exception:
            self.state = 'failed'
            msg = traceback.format_exc()
            self.release_id.message_post(body=f"Deployment of version {self.name} failed: {msg}")
            self.log_release = msg or ''
            if logsio:
                self.log_release += '\n'.join(logsio.get_lines())
            logger.error(msg)
            self.env.cr.commit()
            raise
        finally:
            self.env.cr.commit()

    def _get_ignored_branch_names(self, repo):
        all_releases = self.env['cicd.release'].sudo().search([
            ('branch_id.repo_id', '=', repo.id)
            ])
        ignored_branch_names = []
        ignored_branch_names += list(all_releases.mapped('candidate_branch'))
        ignored_branch_names += list(all_releases.mapped('branch_id.name'))
        return ignored_branch_names

    def _collect_tested_branches(self, repo):
        breakpoint()
        for rec in self:
            if rec.release_type == 'hotfix':
                continue
            if rec.state not in ['new']:
                continue
            if rec.release_type != 'standard':
                continue
            repo = rec.release_id.branch_id.repo_id

            ignored_branch_names = self._get_ignored_branch_names(repo)

            # select from many states:
            # * case: previous release may be failed: technical error, merge conflict
            # * case: state is done but was released at another release, so check again

            branches = self.env['cicd.git.branch'].search([
                ('state', 'in', ['candidate', 'tested', 'release', 'done']), # why so many states
                ('active', '=', True),
                ('block_release', '=', False),
                ('repo_id', '=', repo.id),
                ('name', 'not in', ignored_branch_names),
                ('id', 'not in', (rec.release_id.branch_id).ids),
            ]) | rec.branch_ids

            # remove branches, that are already merged
            branches = rec._filter_out_invalid_branches(branches)
            for branch in list(branches):
                if branch.latest_commit_id in rec.release_id.branch_id.commit_ids:
                    branches -= branch
            rec.branch_ids = [[6, 0, branches.ids]]

    def _filter_out_invalid_branches(self, branches):
        self.ensure_one()
        repo = self.release_id.repo_id
        ignored_branch_names = self._get_ignored_branch_names(repo)
        for b in list(branches):
            if b.state not in ['tested', 'candidate', 'done', 'release']:
                branches -= b
            if b.name in ignored_branch_names:
                branches -= b

            if b.target_release_ids and self.release_id not in b.target_release_ids:
                branches -= b

            if self.env['cicd.release'].search_count([('repo_id', '=', self.release_id.repo_id.id)]) > 1:
                if not b.target_release_ids:
                    branches -= b

        branches -= branches.filtered(lambda x: x.block_release or not x.active)
        return branches

    def recreate_candidate_branch_in_git(self):
        if self.state == 'failed':
            if not self.release_id.item_ids.filtered(lambda x: x.state == 'new'):
                self.state = 'new'
        self.commit_ids = [[6, 0, []]]
        self._recreate_candidate_branch_in_git()

    def _recreate_candidate_branch_in_git(self):
        """
        Heavy function - takes longer and does quite some work.
        """
        breakpoint()
        self.ensure_one()
        if self.state not in ('new'):
            return
            # raise ValidationError("Branches can only be changed in state 'new'.")
        if not self.branch_ids:
            return

        # fetch latest commits:
        with self.release_id._get_logsio() as logsio:
            repo = self.release_id.repo_id.with_context(active_test=False)
            # remove blocked
            self.branch_ids = [[6, 0, self._filter_out_invalid_branches(self.branch_ids).ids]]
            critical_date = self.final_curtain or arrow.get().datetime
            commits = self._get_commits_within_final_curtains(critical_date)
            logsio.info("Identified following commits under final curtain:")
            for commit in commits:
                logsio.info(commit.name)

            if set(commits.ids) == set(self.commit_ids.ids):
                logsio.info("The commits did not change - so a new candidate branch is not created.")
                return

            logsio.info("Commits changed, so creating a new candidate branch")
            try:
                branches = ', '.join(self.mapped('branch_ids.name'))
                # after pull the message_commit is sorted with git log and
                # appears at the top of the branch
                message_commit = repo._recreate_branch_from_commits(
                    commits=commits,
                    target_branch_name=self.release_id.candidate_branch,
                    logsio=logsio,
                    make_info_commit_msg=(
                        f"Release Item {self.id}\n"
                        f"Includes latest commits from:\n{branches}"
                    )
                )
            except RetryableJobError:
                raise

            except Exception as ex:
                msg = traceback.format_exc()
                self.state = 'failed'
                self.release_id.message_post(body=(
                    f"Merging into candidate failed {self.name}\n"
                    f"{ex}\n"
                    f"{msg}\n"
                ))
                self.env.cr.commit()
                if logsio:
                    logsio.error(ex)
                logger.error(ex)
            else:
                if message_commit and commits:
                    message_commit.approval_state = 'approved'
                    self.commit_ids = [[6, 0, commits.ids]]
                    self.commit_id = message_commit
                    candidate_branch = repo.branch_ids.filtered(
                        lambda x: x.name == self.release_id.candidate_branch)
                    candidate_branch.ensure_one()

                    (
                        self.release_id.branch_id
                        | self.branch_ids
                        | candidate_branch
                    )._compute_state()

    def _get_commits_within_final_curtains(self, critical_date):
        commits = self.env['cicd.git.commit']

        for branch in self.branch_ids:
            for commit in branch.commit_ids.sorted(
                    lambda x: x.date, reverse=True):
                if critical_date:
                    if commit.date.strftime("%Y-%m-%d %H:%M:%S") > critical_date.strftime("%Y-%m-%d %H:%M:%S"):
                        continue

                if not commit.force_approved and (commit.test_state != 'success' or commit.approval_state != 'approved'):
                    continue

                commits |= commit

                break
        return commits

    @api.fieldchange("branch_ids")
    def _on_change_branches(self, changeset):
        for rec in self:
            (changeset['branch_ids']['old'] | changeset['branch_ids']['new'])._compute_state()

    def set_to_ignore(self):
        for rec in self:
            if rec.state not in ['failed', 'new']:
                raise ValidationError("Cannot set state to ignore")
            rec.state = 'ignore'

    def reschedule(self):
        for rec in self:
            if rec.state not in ['ignore']:
                raise ValidationError("Cannot set state to new")
            rec.state = 'new'

    def retry(self):
        for rec in self:
            if rec.state in ('failed', 'ignore'):
                rec.state = 'new'
                rec.log_release = False
