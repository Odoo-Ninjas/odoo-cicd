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
    project_name = fields.Char(
        "Project Name", required=True,
        help="techincal name - no special characters")
    repo_id = fields.Many2one(
        "cicd.git.repo", required=True, string="Repo", store=True)
    repo_short = fields.Char(related="repo_id.short")
    branch_id = fields.Many2one(
        'cicd.git.branch', string="Branch", required=True)
    candidate_branch = fields.Char(
        string="Candidate", required=True, default="master_candidate")
    item_ids = fields.One2many(
        'cicd.release.item', 'release_id', string="Release")
    auto_release = fields.Boolean("Auto Release")
    sequence_id = fields.Many2one(
        'ir.sequence', string="Version Sequence", required=True)
    countdown_minutes = fields.Integer("Countdown Minutes")
    last_item_id = fields.Many2one(
        'cicd.release.item', compute="_compute_last")
    state = fields.Selection(related='item_ids.state')
    action_ids = fields.One2many(
        'cicd.release.action', 'release_id', string="Release Actions")
    send_pre_release_information = fields.Boolean(
        "Send Pre-Release Information")

    @api.constrains("project_name")
    def _check_project_name(self):
        for rec in self:
            for c in " !?#/\\+:,":
                if c in rec.project_name:
                    raise ValidationError("Invalid Project-Name")

    @api.depends("item_ids")
    def _compute_last(self):
        for rec in self:
            items = rec.item_ids.with_context(prefetch_fields=False).sorted(
                lambda x: x.id, reverse=True)
            if not items:
                rec.last_item_id = False
            else:
                rec.last_item_id = items[0]

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

    @api.model
    def cron_heartbeat(self):

        for rec in self.search([]):
            last_item = rec.last_item_id
            if last_item.state in ['ready', 'done'] or \
                    'failed_' in last_item.state:
                rec.item_ids = [[0, 0, {}]]

            last_item.cron_heartbeat()

    def make_hotfix(self):
        existing = self.item_ids.with_context(prefetch_fields=False).filtered(lambda x: x.release_type == 'hotfix' and x.state not in ['done', 'failed'])
        if existing:
            raise ValidationError("Hotfix already exists. Please finish it before")
        self.item_ids = [[0, 0, {
            'release_type': 'hotfix',
        }]]

    def toggle_active(self):
        for rec in self:
            rec.active = not rec.active
