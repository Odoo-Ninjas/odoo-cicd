from contextlib import contextmanager
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
import arrow
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.logsio_writer import LogsIOWriter
import logging
logger = logging.getLogger(__name__)


class Release(models.Model):
    _inherit = ['mail.thread', 'mixin.schedule']
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
    item_ids = fields.One2many(
        'cicd.release.item', 'release_id', string="Release")
    auto_release = fields.Boolean("Auto Release")
    sequence_id = fields.Many2one(
        'ir.sequence', string="Version Sequence", required=True)
    countdown_minutes = fields.Integer("Countdown Minutes")
    minutes_to_release = fields.Integer(
        "Max Minutes for release.", default=120)
    last_item_id = fields.Many2one(
        'cicd.release.item', compute="_compute_last")
    next_to_finish_item_id = fields.Many2one(
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
        """
        Next to finish item - what is that:

        If the item becomes "ready" then a new item with state 'collecting'
        is created. So there are two active items hanging around.
        The next todo item is then the second last item in that scenario.

        Needed for calculating branch state.
        """
        for rec in self:
            items = rec.item_ids.with_context(prefetch_fields=False).sorted(
                lambda x: x.id, reverse=True)
            if not items:
                rec.last_item_id = False
                rec.next_to_finish_item_id = False
            else:
                rec.last_item_id = items[0]
                rec.next_to_finish_item_id = items[0]
                if len(items) > 1:
                    if items[1].state == 'ready':
                        rec.next_to_finish_item_id = items[1]

    @api.constrains("branch_id")
    def _check_branches(self):
        for rec in self:
            for field in [
                'branch_id',
            ]:
                if not self[field]:
                    continue
                if self.search_count([
                    ('id', '!=', rec.id),
                    ('repo_id', '=', rec.repo_id.id),
                    (field, '=', rec[field] if isinstance(rec[field], (
                        bool, str)) else rec[field].id),
                ]):
                    raise ValidationError(
                        "Branches must be unique per release!")

    @contextmanager
    def _get_logsio(self):
        with LogsIOWriter.GET(self.repo_id.short, "Release") as logsio:
            yield logsio

    def _ensure_item(self):
        items = self.with_context(prefetch_fields=False).item_ids.sorted(
            lambda x: x.id, reverse=True).filtered(
                lambda x: x. release_type == 'standard')
        if not items or items[0].state in ['done', 'failed']:
            items = self.item_ids.create({
                'release_id': self.id,
            })
        else:
            items = items[0]
        return items

    def _send_pre_release_information(self):
        for rec in self:
            pass
            # import pudb;pudb.set_trace()

    @api.model
    def cron_heartbeat(self):

        for rec in self.search([]):
            last_item = rec.last_item_id
            if last_item.state in [False, 'ready', 'done'] or \
                    'failed_' in last_item.state:
                planned_date = rec._compute_next_date(
                    last_item.planned_maximum_finish_date
                )
                rec.item_ids = [[0, 0, {
                    'planned_date': planned_date,
                }]]

            for item in self.env['cicd.release.item'].search([
                ('state', 'not ilike', 'failed_'),
                ('state', '!=', 'done'),
                ('release_id', '=', rec.id),
            ]):
                item.cron_heartbeat()

    def make_hotfix(self):
        existing = self.item_ids.with_context(prefetch_fields=False).filtered(
            lambda x: x.release_type == 'hotfix' and x.state not in [
                'done', 'failed'])
        if existing:
            raise ValidationError((
                "Hotfix already exists. "
                "Please finish it before"))
        self.item_ids = [[0, 0, {
            'release_type': 'hotfix',
        }]]

    def toggle_active(self):
        for rec in self:
            rec.active = not rec.active
