import os
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from pathlib import Path
import humanize

class Dump(models.Model):
    _inherit = ['cicd.mixin.size']
    _name = 'cicd.dump'

    active = fields.Boolean("Active", default=True)
    name = fields.Char("Name", required=True)
    machine_id = fields.Many2one("cicd.machine", string="Machine", required=True)

    @api.model
    def _cron_update(self):
        for machine in self.env['cicd.machine'].sudo().search([]):
            self._update_dumps(machine)

    @api.constrains("name")
    def _check_name(self):
        for rec in self:
            while rec.name.endswith("/"):
                rec.name = rec.name[:-1]

    def _update_dumps(self, machine):

        with machine._shell() as shell:
            for volume in machine.volume_ids.filtered(lambda x: x.ttype == 'dumps'):
                files = machine._execute_shell([
                    "ls", volume.name + "/"
                ]).output.strip().split("\n")

                for file in files:
                    if not file:
                        continue
                    path = volume.name + "/" + file
                    dumps = self.sudo().with_context(active_test=False).search([('name', '=', path), ('machine_id', '=', machine.id)])
                    if not dumps:
                        dumps = dumps.sudo().create({
                            'name': path,
                            'machine_id': machine.id,
                        })
                    try:
                        machine._execute_shell(['/usr/bin/test', '-f', path])
                    except Exception:
                        import pudb;pudb.set_trace()
                    else:
                        dumps.size = int(machine._execute_shell([
                            'stat', '-c', '%s', path
                        ]).output.strip())