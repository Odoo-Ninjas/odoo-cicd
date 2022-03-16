import traceback
from . import pg_advisory_lock
import psycopg2
import arrow
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError
from ..tools.logsio_writer import LogsIOWriter
from .repository import MergeConflict
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
import logging
logger = logging.getLogger(__name__)


class ReleaseItem(models.Model):
    _inherit = ['mail.thread', 'cicd.open.window.mixin']
    _name = 'cicd.release.item'
    _order = 'id desc'
    _log_access = False

    name = fields.Char("Version")
    repo_id = fields.Many2one(
        'cicd.git.repo', related="release_id.repo_id")
    branch_ids = fields.One2many(
        'cicd.release.item.branch', 'item_id', tracking=True)
    branch_branch_ids = fields.Many2many(
        'cicd.git.branch', compute="_compute_branch_branch_ids")
    item_branch_name = fields.Char(compute="_compute_item_branch_name")
    item_branch_id = fields.Many2one(
        'cicd.git.branch', string="Release Branch")
    release_id = fields.Many2one(
        'cicd.release', string="Release",
        required=True, ondelete="cascade")
    planned_date = fields.Datetime(
        "Planned Deploy Date", tracking=True)
    planned_maximum_finish_date = fields.Datetime(compute="_compute_dates")
    stop_collecting_at = fields.Datetime(compute="_compute_dates")
    done_date = fields.Datetime("Done", tracking=True)
    changed_lines = fields.Integer("Changed Lines", tracking=True)
    log_release = fields.Text("Log", readonly=True)
    state = fields.Selection([
        ("collecting", "Collecting"),
        ('collecting_merge_conflict', 'Collecting Merge Conflict'),
        ('integrating', 'Integration'),
        ('failed_merge', 'Failed: Merge Conflict'),
        ('failed_integration', 'Failed: Integration'),
        ('failed_technically', 'Failed technically'),
        ('failed_too_late', 'Failed: too late'),
        ('failed_user', "Failed: by user"),
        ('failed_merge_master', "Failed: merge on master"),
        ('ready', 'Ready'),
        ('done', 'Done'),
    ], string="State", default='collecting', required=True, tracking=True)
    computed_summary = fields.Text(
        "Computed Summary",
        compute="_compute_summary", tracking=True)
    count_failed_queuejobs = fields.Integer(
        "Failed Jobs", compute="_compute_failed_jobs")
    commit_id = fields.Many2one(
        'cicd.git.commit',
        string="Released commit",
        help=(
            "After merging all tested commits this is the "
            "commit that holds all merged commits."))
    needs_merge = fields.Boolean()
    exc_info = fields.Text("Exception Info")

    release_type = fields.Selection([
        ('standard', 'Standard'),
        ('hotfix', 'Hotfix'),
    ], default="standard", required=True, readonly=True)

    @api.depends(
        'planned_date',
        'release_id.countdown_minutes',
        'release_id.minutes_to_release',
        )
    def _compute_dates(self):
        for rec in self:
            if not rec.planned_date:
                rec.stop_collecting_at = False
                rec.planned_maximum_finish_date = False
            else:
                start_from = arrow.get(rec.planned_date)
                rec.stop_collecting_at = start_from.shift(
                    minutes=-1 * rec.release_id.countdown_minutes
                    ).strftime(DTF)
                rec.planned_maximum_finish_date = start_from.shift(
                    minutes=rec.release_id.minutes_to_release).strftime(DTF)

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
            rec.count_failed_queuejobs = len(
                jobs.filtered(lambda x: x.state == 'failed'))

    @api.model
    def create(self, vals):
        release = self.env['cicd.release'].browse(vals['release_id'])
        vals['name'] = release.sequence_id.next_by_id()
        res = super().create(vals)
        return res

    def _compute_summary(self):
        for rec in self:
            summary = []
            for branch in rec.branch_ids.branch_id.sorted(lambda x: x.date):
                summary.append(f"* {branch.enduser_summary or branch.name}")
            rec.computed_summary = '\n'.join(summary)

    def _do_release(self):
        breakpoint()
        try:
            with self.release_id._get_logsio() as logsio:
                merge_commit_id = self.item_branch_id.latest_commit_id
                errors = self.release_id.action_ids.run_action_set(
                    self, self.release_id.action_ids, merge_commit_id)
                if errors:
                    raise Exception(str(';'.join(map(str, errors))))
                else:
                    self.state = 'done'

                self.log_release = ','.join(logsio.get_lines())
                self._on_done()

        except RetryableJobError:
            raise

        except Exception:
            self.state = 'failed_technically'
            msg = traceback.format_exc()
            self.release_id.message_post(body=(
                "Deployment of "
                f"version {self.name} "
                f"failed: {msg}"))

            self.log_release = msg or ''
            if logsio:
                self.log_release += '\n'.join(logsio.get_lines())
            logger.error(msg)
            self.env.cr.commit()
            raise
        finally:
            self.env.cr.commit()

    def _get_ignored_branch_names(self):
        self.ensure_one()
        all_releases = self.env['cicd.release'].sudo().search([
            ('branch_id.repo_id', '=', self.repo_id.id)
            ])
        ignored_branch_names = []
        ignored_branch_names += list(all_releases.item_ids.mapped(
            'item_branch_name'))
        ignored_branch_names += list(all_releases.branch_id.mapped('name'))
        return ignored_branch_names

    def merge(self):
        """
        Heavy function - takes longer and does quite some work.
        """
        target_branch_name = self.item_branch_name
        self.ensure_one()

        with self.release_id._get_logsio() as logsio:
            logsio.info("Commits changed, so creating a new candidate branch")
            try:
                branches = ', '.join(self.branch_ids.branch_id.mapped('name'))
                try:
                    commits = self.mapped('branch_ids.commit_id')
                    repo = self.repo_id
                    message_commit = repo._recreate_branch_from_commits(
                        source_branch=self.release_id.branch_id.name,
                        commits=commits,
                        target_branch_name=target_branch_name,
                        logsio=logsio,
                        make_info_commit_msg=(
                            f"Release Item {self.id}\n"
                            "Includes latest commits from:\n"
                            f"{branches}"
                        )
                    )
                    item_branch = message_commit.branch_ids.filtered(
                        lambda x: x.name == self.item_branch_name)
                    self.item_branch_id = item_branch
                    self.branch_ids.write({'state': 'merged'})

                except MergeConflict as ex:
                    for commit in ex.conflicts:
                        self.branch_ids.filtered(
                            lambda x: x.commit_id == commit).write({
                                'state': 'conflict'})
                    self.state = 'collecting_merge_conflict'
                    return

                self.needs_merge = False
                assert self.item_branch_id

            except RetryableJobError:
                raise

            except Exception as ex:
                self.state = 'collecting_merge_conflict'
                self.exc_info = str(ex)
                if logsio:
                    logsio.error(ex)
                logger.error(ex)
            else:
                if message_commit and commits:
                    message_commit.approval_state = 'approved'
                    self.commit_id = message_commit
                    candidate_branch = repo.branch_ids.filtered(
                        lambda x: x.name == self.item_branch_name)
                    candidate_branch.ensure_one()
                    self.item_branch_id = candidate_branch
                    candidate_branch._compute_state()

                self.mapped('branch_ids.branch_id')._compute_state()

    @api.fieldchange("branch_ids")
    def _on_change_branches(self, changeset):
        for rec in self:
            branches = (
                changeset['branch_ids']['old']
                | changeset['branch_ids']['new'])
            branches.branch_id._compute_state()

    def abort(self):
        for rec in self:
            if rec.state == 'done':
                raise ValidationError("Cannot set a done release to fail")
            rec.state = 'failed_user'

    def _lock(self):
        try:
            self.env.cr.execute((
                "select id "
                "from cicd_release "
                "where id=%s for update nowait"
            ), (self.release_id.id,))
        except psycopg2.errors.LockNotAvailable as ex:
            raise RetryableJobError((
                "Could not work exclusivley "
                f"on release {self.release_id.id} - retrying in few seconds",
            ), ignore_retry=True, seconds=15) from ex

    def cron_heartbeat(self):
        breakpoint()
        self.ensure_one()
        self._lock()
        now = fields.Datetime.now()
        deadline = self.planned_maximum_finish_date

        if deadline < now:
            if 'failed_' not in self.state:
                if self.state not in ['done']:
                    self.state = 'failed_too_late'
                    return

        if self.state == ['collecting', 'collecting_merge_conflict']:
            self._collect()
            if self.needs_merge or not self.item_branch_id:
                self.merge()

            if self.stop_collecting_at < now:
                if not self.branch_ids:
                    self.state = 'done'
                else:
                    states = self.branch_ids.mapped('state')
                    if 'candidate' in states and 'conflict' not in states:
                        self.state = 'failed_too_late'
                    elif not all(x == 'merged' for x in states):
                        self.state = 'failed_merge'
                    else:
                        self.state = 'integrating'

        elif self.state == 'integrating':
            # check if test done
            runs = self.item_branch_id.latest_commit_id.test_run_ids
            open_runs = runs.filtered(
                lambda x: x.state not in ['failed', 'success'])
            success = 'success' in runs.mapped('state')

            # TODO make sure quality assurance
            if not success and not open_runs:
                self.item_branch_id.run_tests(silent=True)

            elif success:
                try:
                    self._merge_on_master()
                except Exception as ex:
                    self.exc_info = str(ex)
                    self.state = 'failed_merge_master'
                else:
                    self.state = 'ready'

        elif self.state == 'ready':
            if now > self.planned_date:
                self._do_release()

        elif self.state == 'done':
            pass

        elif 'failed_' in self.state:
            pass

        else:
            raise NotImplementedError(self.state)

    def _merge_on_master(self):
        """
        Merges
        """
        breakpoint()
        logsio = None
        self._lock()

        with self.release_id._get_logsio() as logsio:

            release = self.release_id
            repo = self.repo_id

            candidate_branch = self.item_branch_id
            candidate_branch.ensure_one()
            if not candidate_branch.active:
                raise UserError((
                    "Candidate branch "
                    f"'{self.item_branch_id.name}'"
                    "is not active!"))

            tag = (
                f'{repo.release_tag_prefix}{self.name}-'
                f'{fields.Datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
            )
            changed_lines, merge_commit_id = repo._merge(
                self.commit_id,
                release.branch_id,
                set_tags=[tag],
                logsio=logsio,
            )
            self.changed_lines = changed_lines

    def _collect(self):
        for rec in self:
            ignored_branch_names = rec._get_ignored_branch_names()
            branches = self.env['cicd.git.branch'].search([
                ('repo_id', '=', rec.repo_id.id),
                ('block_release', '=', False),
                ('name', 'not in', ignored_branch_names),
                ('state', 'in', ['tested', 'candidate']),
            ])

            def _keep_undeployed_commits(branch):
                done_items = self.release_id.item_ids.filtered(
                    lambda x: x.state == 'done')
                done_commits = done_items.branch_ids.mapped('commit_id')
                return branch.latest_commit_id not in done_commits

            branches = branches.filtered(_keep_undeployed_commits)

            for branch in branches:
                existing = rec.branch_ids.filtered(
                    lambda x: x.branch_id == branch)
                if not existing:
                    rec.branch_ids = [[0, 0, {
                        'branch_id': branch.id,
                    }]]
                    rec.needs_merge = True

                elif existing.commit_id != branch.latest_commit_id:
                    existing.commit_id = branch.latest_commit_id
                    rec.needs_merge = True

            for existing in rec.branch_ids:
                if existing.branch_id not in branches:
                    existing.unlink()
                    rec.needs_merge = True

    def _compute_item_branch_name(self):
        for rec in self:
            rec.item_branch_name = (
                "release_"
                f"{rec.release_id.branch_id.name}_"
                f"{rec.id}"
            )

    def retry(self):
        for rec in self:
            rec.log_release = False
            rec.exc_info = False

            if rec.state == 'failed_technically':
                rec.state = 'ready'
                rec.planned_date = fields.Datetime.now()

            elif rec.state == 'collecting_merge_conflict':
                rec.state = 'collecting'
                rec.needs_merge = True

            else:
                raise NotImplementedError(rec.state)

    def _compute_branch_branch_ids(self):
        for rec in self:
            rec.branch_branch_ids = rec.branch_ids.mapped('branch_id')

    def release_now(self):
        if self.state not in ['collecting', 'ready', 'failed_too_late']:
            raise ValidationError("Invalid state to switch from.")
        self.planned_date = fields.Datetime.now()
        self.state = 'collecting'