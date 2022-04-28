from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError


class ItemBranch(models.Model):
    _name = 'cicd.release.item.branch'

    item_id = fields.Many2one(
        'cicd.release.item', string="Item", required=True)
    branch_id = fields.Many2one(
        'cicd.git.branch', string="Branch", required=True)
    commit_id = fields.Many2one(
        'cicd.git.commit', string="Commit", required=True)
    state = fields.Selection([
        ('candidate', 'Candidate'),
        ('merged', 'Merged'),
        ('already_merged', 'Already Merged'),
        ('conflict', 'Conflict'),
    ], string="State", default="candidate")
    commit_date = fields.Datetime(related="commit_id.date")
    is_merged = fields.Boolean(compute="_is_merged", store=True)

    @api.onchange('branch_id')
    def _onchange_branch_id(self):
        for rec in self:
            rec.commit_id = rec.branch_id.latest_commit_id

    @api.depends('state')
    def _is_merged(self):
        for rec in self:
            rec.is_merged = 'merged' in rec.state

    @api.constrains("commit_id", "branch_id")
    def _check_branch_commit(self):
        for rec in self:
            if rec.commit_id and rec.branch_id:
                if rec.commit_id not in rec.branch_id.commit_ids:
                    raise ValidationError("Commit not part of branch")

    _sql_constraints = [
        ('item_id_branch_id_unique', "unique(item_id, branch_id)",
        _("Only one unique entry allowed.")),
    ]

    @api.model
    def create(self, vals):
        if not vals.get('commit_id'):
            branch = self.env['cicd.git.branch'].browse(vals['branch_id'])
            vals['commit_id'] = branch.latest_commit_id.id
        return super().create(vals)

    @api.recordchange('commit_id')
    def _updated_commit(self):
        for rec in self:
            rec.state = 'candidate'

    def open_window(self):
        return super().open_window({
            'res_model': self.commit_id._name,
            'res_id': self.commit_id.id,
        })

    def view_changes(self):
        return self.commit_id.view_changes()