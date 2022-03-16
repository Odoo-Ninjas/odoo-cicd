from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
class ItemBranch(models.Model):
    _name = 'cicd.release.item.branch'

    item_id = fields.Many2one('cicd.release.item', string="Item")
    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    commit_id = fields.Many2one('cicd.git.commit', string="Commit")

    @api.constrains("commit_id", "branch_id")
    def _check_branch_commit(self):
        for rec in self:
            if rec.commit_id and rec.branch_id:
                if rec.commit_id not in rec.branch_id.commit_ids:
                    raise ValidationError("Commit not part of branch")