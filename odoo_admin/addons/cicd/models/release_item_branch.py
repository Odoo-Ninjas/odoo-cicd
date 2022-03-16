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
        ('conflict', 'Conflict'),
    ], string="State", default="candidate")

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