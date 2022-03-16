from contextlib import contextmanager
import traceback
import arrow
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.logsio_writer import LogsIOWriter
import logging
logger = logging.getLogger(__name__)
class Release(models.Model):
    _inherit = ['mail.thread']
    _name = 'cicd.release'

    active = fields.Boolean("Active", default=True, store=True)
    name = fields.Char("Name", required=True)
    project_name = fields.Char("Project Name", required=True, help="techincal name - no special characters")
    repo_id = fields.Many2one("cicd.git.repo", required=True, string="Repo", store=True)
    repo_short = fields.Char(related="repo_id.short")
    branch_id = fields.Many2one('cicd.git.branch', string="Branch", required=True)
    candidate_branch = fields.Char(string="Candidate", required=True, default="master_candidate")
    item_ids = fields.One2many('cicd.release.item', 'release_id', string="Release")
    auto_release = fields.Boolean("Auto Release")
    sequence_id = fields.Many2one('ir.sequence', string="Version Sequence", required=True)
    countdown_minutes = fields.Integer("Countdown Minutes")
    is_latest_release_done = fields.Boolean("Latest Release Done", compute="_compute_latest_release_done")
    state = fields.Selection(related='item_ids.state')
    planned_timestamp_after_preparation = fields.Integer("Release after preparation in minutes", default=60)
    action_ids = fields.One2many('cicd.release.action', 'release_id', string="Release Actions")
    send_pre_release_information = fields.Boolean("Send Pre-Release Information")

    def toggle_active(self):
        for rec in self:
            rec.active = not rec.active

    @api.constrains("project_name")
    def _check_project_name(self):
        for rec in self:
            for c in " !?#/\\+:,":
                if c in rec.project_name:
                    raise ValidationError("Invalid Project-Name")

    def make_hotfix(self):
        existing = self.item_ids.with_context(prefetch_fields=False).filtered(lambda x: x.release_type == 'hotfix' and x.state not in ['done', 'failed'])
        if existing:
            raise ValidationError("Hotfix already exists. Please finish it before")
        self.item_ids = [[0, 0, {
            'release_type': 'hotfix',
        }]]

    def _compute_latest_release_done(self):
        for rec in self:
            items = rec.item_ids.with_context(prefetch_fields=False).sorted(lambda x: x.create_date, reverse=True)
            if not items:
                rec.is_latest_release_done = False
            else:
                rec.is_latest_release_done = items[0].date_done

    @api.constrains("candidate_branch", "branch_id")
    def _check_branches(self):
        for rec in self:
            for field in [
                'candidate_branch',
                'branch_id',
            ]:
                if not self[field]:
                    continue
                if self.search_count([
                    ('id', '!=', rec.id),
                    ('repo_id', '=', rec.repo_id.id),
                    (field, '=', rec[field] if isinstance(rec[field], (bool, str)) else rec[field].id),
                ]):
                    raise ValidationError("Branches must be unique per release!")

    def _cron_schedule_release_times(self):
        for self in self:
            self.ensure_one()
            new_items = self.with_context(prefetch_fields=False).item_ids.filtered(lambda x: x.state == 'new')
            final_curtain_dt = arrow.get().shift(minutes=self.countdown_minutes).strftime("%Y-%m-%d %H:%M:%S")
            if not new_items:
                new_items = self.item_ids.create({
                    'release_id': self.id,
                    'release_type': 'standard',
                    'final_curtain': final_curtain_dt,
                    'planned_date': arrow.get().shift(minutes=self.planned_timestamp_after_preparation).strftime("%Y-%m-%d %H:%M:%S"),
                })

    @contextmanager
    def _get_logsio(self):
        with LogsIOWriter.GET(self.repo_id.short, "Release") as logsio:
            yield logsio

    def _ensure_item(self):
        items = self.with_context(prefetch_fields=False).item_ids.sorted(lambda x: x.id, reverse=True).filtered(lambda x: x. release_type == 'standard')
        if not items or items[0].state in ['done', 'failed']:
            items = self.item_ids.create({
                'release_id': self.id,
            })
        else:
            items = items[0]
        return items

    def do_release_if_planned(self):
        breakpoint()
        for rec in self:
            item = rec.item_ids.with_context(prefetch_fields=False).filtered(lambda x: x.state in ('new')).sorted(lambda x: x.id)
            if not item:
                continue
            item = item[0]
            if item.planned_date > fields.Datetime.now():
                continue

            item._trigger_do_release()

    def collect_tested_branches(self):
        breakpoint()
        for rec in self:
            new_ones = rec.item_ids.with_context(prefetch_fields=False).filtered(lambda x: x.state in ('new'))
            if not new_ones:
                items = rec.item_ids.with_context(prefetch_fields=False).filtered(lambda x: x.state != 'ignore')
                if not items or items[0].planned_date < fields.Datetime.now():
                    # check if the last deploy date passed
                    new_ones = new_ones.create({'release_id': rec.id})
            new_ones._collect_tested_branches(self.repo_id)
            for new_one in new_ones:
                new_one.with_delay(
                    identity_key=f"recreate_branch_release_item_id{new_one.id}"
                    )._recreate_candidate_branch_in_git()

    def _technically_do_release(self, release_item, merge_commit_id):
        """
        merge_commit_id: after merging the main branch with the candidate branch
        a new commit is created.
        """

        errors = self.action_ids.run_action_set(release_item, self.action_ids, merge_commit_id)
        return errors

    def _send_pre_release_information(self):
        for rec in self:
            pass
            # import pudb;pudb.set_trace()
