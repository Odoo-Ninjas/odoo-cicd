import json
import traceback
from . import pg_advisory_lock
import os
import arrow
import traceback
from odoo import _, api, fields, models, SUPERUSER_ID, registry
from odoo import registry
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from contextlib import contextmanager

class Task(models.Model):
    _name = 'cicd.task'
    _order = 'date desc'

    model = fields.Char("Model")
    res_id = fields.Integer("ID")
    display_name = fields.Char(compute="_compute_display_name")
    machine_id = fields.Many2one('cicd.machine', string="Machine", readonly=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    name = fields.Char("Name")
    date = fields.Datetime("Date", default=lambda self: fields.Datetime.now(), readonly=True)
    is_done = fields.Boolean(compute="_compute_is_done", store=True)

    state = fields.Selection(related='queue_job_id.state', string="State")
    log = fields.Text("Log", readonly=True)
    error = fields.Text("Exception", compute="_compute_error")
    dump_used = fields.Char("Dump used", readonly=True)
    duration = fields.Integer("Duration [s]", readonly=True)
    commit_id = fields.Many2one("cicd.git.commit", string="Commit", readonly=True)
    queue_job_id = fields.Many2one('queue.job', string="Queuejob")
    kwargs = fields.Text("KWargs")
    identity_key = fields.Char()

    @api.depends('queue_job_id', 'queue_job_id.state')
    def _compute_error(self):
        for rec in self:
            faileds = rec.queue_job_id.filtered(lambda x: x.state in ['failed']).sorted(lambda x: x.id, reverse=True)
            if faileds:
                rec.error = faileds.exc_info
            else:
                rec.error = False

    @api.depends('state')
    def _compute_is_done(self):
        for rec in self:
            rec.is_done = rec.state in ['done', 'failed']


    def _compute_display_name(self):
        for rec in self:
            name = rec.name
            name = name.replace("obj.", "")
            if name.startswith("_"):
                name = name[1:]
            name = name.split("(")[0]
            rec.display_name = name

    def perform(self, now=False):
        self.ensure_one()

        if not now:
            queuejob = self.with_delay(
                identity_key=self._get_identity_key(),
                eta=arrow.get().shift(seconds=10).strftime("%Y-%m-%d %H:%M:%S"),
            )._exec(now)
            if queuejob:
                self.sudo().queue_job_id = self.env['queue.job'].prefix(queuejob, ":".join(filter(bool, [self.branch_id.name, self.name, self.identity_key])))
        else:
            self._exec(now)

    def _get_identity_key(self):
        if self.identity_key:
            return self.identity_key
        name = self._get_short_name()
        return f"{self.branch_id.project_name}_{name}"

    def _get_short_name(self):
        name = self.name or ''
        if name.startswith("_"):
            name = name[1:]
        return name

    def _exec(self, now):
        self = self.sudo()
        db_registry = registry(self.env.cr.dbname)
        with db_registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            started = arrow.get()
            self = self.with_env(env)
            # try nicht unbedingt notwendig; bei __exit__ wird ein close aufgerufen
            with self.branch_id._get_new_logsio_instance(self._get_short_name()) as logsio:
                with pg_advisory_lock(self.env.cr, f"perform_task_{self.branch_id.id}_{self.branch_id.project_name}"):  # project name so that tests may run parallel to backups
                    try:
                        dest_folder = self.machine_id._get_volume('source') / self.branch_id.project_name
                        with self.machine_id._shell(cwd=dest_folder, logsio=logsio, project_name=self.branch_id.project_name) as shell:
                            self.branch_id.repo_id._get_main_repo(
                                destination_folder=dest_folder,
                                machine=self.machine_id,
                                limit_branch=self.branch_id.name,
                                )
                            obj = self.env[self.model].sudo().browse(self.res_id)
                            # mini check if it is a git repository:
                            try:
                                shell.X(["git", "status"])
                            except Exception:
                                msg = traceback.format_exc()
                                raise Exception(f"Directory seems to be not a valid git directory: {dest_folder}\n{msg}")

                            sha = shell.X(["git", "log", "-n1", "--format=%H"])['stdout'].strip()
                            commit = self.branch_id.commit_ids.filtered(lambda x: x.name == sha)

                            # if not commit:
                            #     raise ValidationError(f"Commit {sha} not found in branch.")
                            # get current commit
                            args = {
                                'task': self,
                                'logsio': logsio,
                                'shell': shell,
                                }
                            if self.kwargs and self.kwargs != 'null':
                                args.update(json.loads(self.kwargs))
                            exec('obj.' + self.name + "(**args)", {'obj': obj, 'args': args})
                            self.sudo().commit_id = commit

                    except Exception:
                        msg = traceback.format_exc()
                        log = '\n'.join(logsio.get_lines())

                        raise Exception(f"{msg}\n\n{log}")

                    self.log = '\n'.join(logsio.get_lines())

                    duration = (arrow.get() - started).total_seconds()
                    self.duration = duration
                    if logsio:
                        logsio.info(f"Finished after {duration} seconds!")

    @api.model
    def _cron_cleanup(self):
        dt = arrow.get().shift(days=-10).strftime("%Y-%m-%d %H:%M:%S")
        self.search([
            ('create_date', '<', dt)
            ]).unlink()

    def requeue(self):
        for rec in self.filtered(lambda x: x.state in ['failed']):
            rec.queue_job_id.requeue()
