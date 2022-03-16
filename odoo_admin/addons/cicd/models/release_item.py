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
    repo_id = fields.Many2one(
        'cicd.git.repo', related="release_id.repo_id")
    branch_ids = fields.One2many(
        'cicd.release.item.branch', 'item_id', tracking=True)
    item_branch_name = fields.Char(compute="_compute_item_branch_name")
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
    ], string="State", default='collecting', required=True, tracking=True)
    computed_summary = fields.Text("Computed Summary", compute="_compute_summary", tracking=True)
    count_failed_queuejobs = fields.Integer("Failed Jobs", compute="_compute_failed_jobs")
    try_counter = fields.Integer("Try Counter", tracking=True)
    commit_id = fields.Many2one('cicd.git.commit', string="Released commit", help="After merging all tested commits this is the commit that holds all merged commits.")
    needs_merge = fields.Boolean()

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

    def _get_ignored_branch_names(self):
        self.ensure_one()
        all_releases = self.env['cicd.release'].sudo().search([
            ('branch_id.repo_id', '=', self.repo_id.id)
            ])
        ignored_branch_names = []
        ignored_branch_names += list(all_releases.mapped('candidate_branch'))
        ignored_branch_names += list(all_releases.mapped('branch_id.name'))
        return ignored_branch_names

    def _merge(self):
        """
        Heavy function - takes longer and does quite some work.
        """
        breakpoint()
        self.ensure_one()
        target_branch_name = self.item_branch_name
        self.ensure_one()

        with self.release_id._get_logsio() as logsio:
            logsio.info("Commits changed, so creating a new candidate branch")
            try:
                branches = ', '.join(self.mapped('branch_ids.name'))
                hier weiter
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

        self.needs_merge = False

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

    def cron_heartbeat(self):
        self.ensure_one()

        if self.state == 'collecting':
            self._collect()
            if self.needs_merge:
                self.merge()
        elif 'failed_' in self.state:
            pass
            
        else:
            raise NotImplementedError()k

    def _collect(self):
        breakpoint()
        for rec in self:
            ignored_branch_names = rec._get_ignored_branch_names()
            branches = self.env['cicd.git.branch'].search([
                ('repo_id', '=', rec.repo_id.id),
                ('block_release', '=', False),
                ('active', '=', True),
                ('name', 'not in', ignored_branch_names),
                ('state', 'in', ['tested', 'candidate']),
            ])

            def _keep_undeployed_commits(branch):
                done_items = self.release_id.item_ids.filtered(
                    lambda x: x.state == 'done')
                done_commits = done_items.mapped('branch_ids.commit_ids')
                return branch.last_commit_id not in done_commits

            branches = branches.filtered(_keep_undeployed_commits)

            for branch in branches:
                existing = rec.branch_ids.filtered(
                    lambda x: x.branch_id == branch)
                if not existing:
                    rec.branch_ids = [[0, 0, {
                        'branch_id': branch.id,
                    }]]
                    rec.needs_merge = True

                elif existing.commit_id != branch.last_commit_id:
                    existing.commit_id = branch.last_commit_id
                    rec.needs_merge = True

            for existing in rec.branch_ids:
                if existing.branch_id not in branches:
                    existing.unlink()
                    rec.needs_merge = True

    def do_release_if_planned(self):
        breakpoint()
        for rec in self:
            item = rec.item_ids.with_context(prefetch_fields=False).filtered(
                lambda x: x.state in ('new')).sorted(lambda x: x.id)
            if not item:
                continue
            item = item[0]
            if item.planned_date > fields.Datetime.now():
                continue

            item._trigger_do_release()

    def _compute_item_branch_name(self):
        for rec in self:
            rec.item_branch_name = (
                "release_"
                f"{rec.relase_id.branch_id.name}_"
                f"{rec.id}"
            )