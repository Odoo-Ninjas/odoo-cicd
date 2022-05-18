import re
from curses import wrapper
import arrow
from contextlib import contextmanager, closing
import base64
import datetime
from . import pg_advisory_lock
import traceback
import time
from odoo import _, api, fields, models
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import ValidationError
import logging
from pathlib import Path
from contextlib import contextmanager


class CicdTestRunLine(models.Model):
    _inherit = 'cicd.open.window.mixin'
    _name = 'cicd.test.run.line'
    _order = 'started desc'

    ttype = fields.Selection([
        ('preparation', "Preparation"),
        ('unittest', 'Unit-Test'),
        ('robottest', 'Robot-Test'),
        ('migration', 'Migration'),
        ('emptydb', 'Migration'),
        ('log', "Log-Note"),
    ], string="Category")
    position = fields.Char("Pos")
    odoo_module = fields.Char("Odoo Module")
    name = fields.Char("Name")
    name_short = fields.Char(compute="_compute_name_short")
    run_id = fields.Many2one('cicd.test.run', string="Run")
    exc_info = fields.Text("Exception Info")
    duration = fields.Integer("Duration")
    state = fields.Selection([
        ('open', 'Open'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], default='open', required=True)
    force_success = fields.Boolean("Force Success")
    started = fields.Datetime(
        "Started", default=lambda self: fields.Datetime.now())
    try_count = fields.Integer("Try Count")
    robot_output = fields.Binary("Robot Output", attachment=True)
    hash = fields.Char("Hash", help="For using")
    reused = fields.Boolean("Reused", readonly=True)

    def _compute_name_short(self):
        for rec in self:
            MAX = 80
            if len(rec.name or '') > MAX:
                rec.name_short = f"{rec.name[:MAX]}..."
            else:
                rec.name_short = rec.name

    def toggle_force_success(self):
        self.sudo().force_success = not self.sudo().force_success

    @api.recordchange('force_success')
    def _onchange_force(self):
        for rec in self:
            if rec.run_id.state not in ['running']:
                rec.run_id._compute_success_rate()

    def robot_results(self):
        return {
            'type': 'ir.actions.act_url',
            'url': f'/robot_output/{self.id}',
            'target': 'new'
        }

    @api.model
    def check_if_test_already_succeeded(
        self, testrun, name, hash,
    ):
        """
        Compares the hash of the module with an existing
        previous run with same hash.
        """
        res = self.search([
            ('run_id.branch_ids.repo_id', '=', testrun.branch_ids.repo_id.id),
            ('name', '=', name),
            ('hash', '=', hash),
            ('state', '=', 'success'),
        ], limit=1, order='id desc')
        if not res:
            return False

        self.create({
            'run_id': testrun.id,
            'state': 'success',
            'name': name,
            'hash': hash,
            'ttype': res.ttype,
            'reused': True,
        })

        return True

    @api.constrains("ttype")
    def _be_graceful(self):
        for rec in self:
            if rec.ttype != 'error':
                continue

            exc_info = rec.exc_info or ''
            name = rec.name or ''

            grace = [
                '.git/index.lock'
            ]
            for vol in self.env['cicd.machine.volume'].search([]):
                grace += [f"{vol.name}.*No such file or directory"]
                grace += [f"No such file or directory.*{vol.name}"]
            for grace in grace:
                for line in (exc_info + name).splitlines():
                    if re.findall(grace, line):
                        rec.ttype = 'log'
                        break
    @api.model
    def create(self, vals):
        res = super().create(vals)
        res._be_graceful()
        return res
