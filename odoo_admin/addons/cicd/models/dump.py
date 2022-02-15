from odoo import _, api, fields, models
import arrow
import logging
logger = logging.getLogger(__name__)

class Dump(models.Model):
    _inherit = ['cicd.mixin.size']
    _name = 'cicd.dump'
    _order = 'date_modified desc'

    active = fields.Boolean("Active", default=True)
    name = fields.Char("Name", required=True, readonly=True)
    machine_id = fields.Many2one("cicd.machine", string="Machine", required=True, readonly=True)
    date_modified = fields.Datetime("Date Modified", readonly=True)

    def download(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            #'url': 'http://www.mut.de',
            'url': f'/download/dump/{self.id}',
            'target': 'new'
        }
        
    def unlink(self):
        for rec in self:
            with self.machine_id._shell() as shell:
                shell.rm(rec.name)

        return super().unlink()

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
            for volume in machine.volume_ids.filtered(lambda x: x.ttype in ['dumps', 'dumps_in']):
                with machine._shell() as shell:
                    splitter = "_____SPLIT_______"
                    files = shell.X([
                        "find", volume.name,
                        "-maxdepth", "1",
                        "-printf", f"%f{splitter}%TY%Tm%Td %TH%TM%TS{splitter}%s\\n",
                    ])['stdout'].strip().split("\n")
                    volname = volume.name
                    if not volname.endswith("/"):
                        volname += "/"

                    Files = {}
                    for line in files:
                        filename, date, size = line.split(splitter)
                        if filename.endswith("/"):
                            continue
                        date = arrow.get(date[:15])
                        path = volname + filename
                        Files[path] = {
                            'date': date.strftime("%Y-%m-%d %H:%M:%S"),
                            'size': int(size),
                        }
                        del path, date, filename, size, line

                    for filepath, file in Files.items():

                        dumps = self.sudo().with_context(active_test=False).search([
                            ('name', '=', filepath),
                            ('machine_id', '=', machine.id)
                            ])
                        if not dumps:
                            dumps = dumps.sudo().create({
                                'name': filepath,
                                'machine_id': machine.id,
                            })

                        dumps.ensure_one()
                        dumps.date_modified = file['date']
                        dumps.size = file['size']

                    breakpoint()
                    for dump in dumps.search([('name', 'like', volname)]):
                        if dump.name.startswith(volname):
                            if dump.name not in Files:
                                dump.unlink()