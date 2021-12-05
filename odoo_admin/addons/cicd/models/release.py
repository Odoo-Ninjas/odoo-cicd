from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.logsio_writer import LogsIOWriter
class Release(models.Model):
    _inherit = ['mail.thread']
    _name = 'cicd.release'

    name = fields.Char("Name", required=True)
    machine_ids = fields.Many2many('cicd.machine', string="Machines")
    repo_id = fields.Many2one(related="branch_id.repo_id", string="Repo", store=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Branch", required=True)
    candidate_branch_id = fields.Many2one('cicd.git.branch', string="Candidate", required=True)
    item_ids = fields.One2many('cicd.release.item', 'release_id', string="Release")
    auto_release = fields.Boolean("Auto Release")
    auto_release_cronjob_id = fields.Many2one('ir.cron', string="Scheduled Release")
    sequence_id = fields.Many2one('ir.sequence', string="Version Sequence", required=True)
    countdown_minutes = fields.Integer("Countdown Minutes")
    is_latest_release_done = fields.Boolean("Latest Release Done", compute="_compute_latest_release_done")
    state = fields.Selection(related='item_ids.state')

    def _compute_latest_release_done(self):
        for rec in self:
            items = rec.item_ids.sorted(lambda x: x.create_date, reverse=True)
            if not items:
                rec.is_latest_release_done = False
            else:
                rec.is_latest_release_done = items[0].date_done

    @api.constrains("candidate_branch_id", "branch_id")
    def _check_branches(self):
        for rec in self:
            for field in [
                'candidate_branch_id',
                'branch_id',
            ]:
                if not self[field]:
                    continue
                if self.search_count([
                    ('id', '!=', rec.id),
                    (field, '=', rec[field].id),
                ]):
                    raise ValidationError("Branches must be unique per release!")


    @api.recordchange('auto_release')
    def _onchange_autorelease(self):
        for rec in self:
            if not rec.auto_release and rec.auto_release_cronjob_id:
                rec.auto_release_cronjob_id.sudo().unlink()
            elif rec.auto_release and not rec.auto_release_cronjob_id:
                rec._make_cronjob()

    def _make_cronjob(self):
        models = self.env['ir.model'].search([('model', '=', self._name)])
        self.auto_release_cronjob_id = self.env['ir.cron'].create({
            'name': self.name + " scheduled release",
            'model_id': models.id,
            'code': f'model.browse({self.id})._cron_prepare_release()'
        })

    def _cron_prepare_release(self):
        self.ensure_one()
        if self.item_ids.filtered(lambda x: x.state == 'new'):
            return
        self.item_ids = [[0, 0, {
            'release_type': 'standard',
        }]]

    def _get_logsio(self):
        logsio = LogsIOWriter(self.repo_id.short, "Release")
        return logsio

    def collect_branches_on_candidate(self):
        logsio = self._get_logsio()
        item = self._ensure_item()
        self.repo_id._collect_branches(
            source_branches=item.branch_ids,
            target_branch=self.candidate_branch_id,
            logsio=logsio,
        )

    def _ensure_item(self):
        items = self.item_ids.sorted(lambda x: x.id, reverse=True)
        if not items or items[0].state in ['done', 'failed']:
            items = self.item_ids.create({
            })
        else:
            items = items[0]
        return items

    def do_release(self):
        self.ensure_one()
        logsio = self._get_logsio()
        self.ensure_item()
        for machine in self.machine_ids:
            res = self.repo_id._merge(
                self.release_id.candidate_branch_id,
                self.release_id.branch_id,
            )
            if not res.diffs_exists:
                self._on_done()
                continue

            raise NotImplementedError("Go to machine pull and update")


class ReleaseItem(models.Model):
    _name = 'cicd.release.item'
    _order = 'id desc'

    name = fields.Char("Version")
    release_id = fields.Many2one('cicd.release', string="Release")
    planned_date = fields.Datetime("Planned Deploy Date", default=lambda self: fields.Datetime.now())
    done_date = fields.Datetime("Done")
    changed_lines = fields.Integer("Changed Lines")
    final_curtain = fields.Datetime("Final Curtains")

    diff_commit_ids = fields.Many2many('cicd.git.commit', string="New Commits", compute="_compute_diff_commits", help="Commits that are new since the last release")
    state = fields.Selection([
        ("new", "New"),
        ("ready", "Ready"),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], string="State")
    computed_summary = fields.Text("Computed Summary", compute="_compute_summary")
    commit_ids = fields.Many2many('cicd.git.commit', string="Commits", help="Commits that are released.")
    branch_ids = fields.Many2one('cicd.git.branch', string="Merged Branches")

    release_type = fields.Selection([
        ('standard', 'Standard'),
        ('hotfix', 'Hotfix'),
    ], default="standard", required=True, readonly=True)


    def on_done(self):
        if not self.changed_lines:
            msg = "Nothing new to deploy"
        msg = '\n'.join(filter(bool, self.mapped('commit_ids.branch_ids.enduser_summary')))
        self.release_id.message_post(body=msg)
        self.done_date = fields.Datetime.now()
    
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
                summary.append(f"* {branch.enduser_summary}")
            rec.computed_summary = '\n'.join(summary)

    def _compute_diff_commits(self):
        for rec in self:
            previous_release = self.release_id.item_ids.filtered(
                lambda x: x.id < rec.id).sorted(
                    lambda x: x.id, reverse=True)
            if not previous_release:
                rec.diff_commit_ids = [[6, 0, []]]
            else:
                rec.diff_commit_ids = [[6, 0, (rec.commit_ids - previous_release[0].commit_ids).ids]]
