import json
import traceback
from . import pg_advisory_lock
import arrow
import traceback
from contextlib import contextmanager, closing
import logging
from odoo.addons.queue_job.exception import RetryableJobError
from odoo import _, api, fields, models, SUPERUSER_ID, registry
from odoo import registry
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from odoo.addons.queue_job.models.queue_job import STATES
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT
logger = logging.getLogger('cicd_task')


class Task(models.Model):
    _name = 'cicd.task'
    _order = 'date desc'

    model = fields.Char("Model")
    res_id = fields.Integer("ID")
    display_name = fields.Char(
        compute="_compute_display_name", store=True)
    machine_id = fields.Many2one(
        'cicd.machine', string="Machine", readonly=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Branch")
    name = fields.Char("Name")
    date = fields.Datetime(
        "Date", default=lambda self: fields.Datetime.now(), readonly=True)
    is_done = fields.Boolean(
        compute="_compute_is_done", store=False, prefetch=False)

    state = fields.Selection(selection=STATES, string="State")
    log = fields.Text("Log", readonly=True)
    error = fields.Text("Exception", compute="_compute_state", prefetch=False)
    dump_used = fields.Char("Dump used", readonly=True)
    duration = fields.Integer("Duration [s]", readonly=True)
    commit_id = fields.Many2one(
        "cicd.git.commit", string="Commit", readonly=True)
    queuejob_uuid = fields.Char("Queuejob UUID")
    queue_job_id = fields.Many2one(
        'queue.job', compute="_compute_queuejob", prefetch=False)
    testrun_id = fields.Many2one('cicd.test.run')

    kwargs = fields.Text("KWargs")
    identity_key = fields.Char()

    def _compute_state(self):
        for rec in self:
            if not rec.queuejob_uuid:
                rec.state = False
                rec.error = False
                continue

            qj = self.env['queue.job'].sudo().search([(
                'uuid', '=', rec.queuejob_uuid)], limit=1)
            if not qj:
                # keep last state as queuejobs are deleted from time to time
                pass
            else:
                rec.state = qj.state
                rec.error = qj.exc_info

    @api.depends('state')
    def _compute_is_done(self):
        for rec in self:
            rec.is_done = rec.state in ['done', 'failed'] \
                if rec.state else False

    @api.depends('name')
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
            job = self.with_delay(
                identity_key=self._get_identity_key(),
                eta=arrow.get().shift(seconds=10).strftime(
                    DEFAULT_SERVER_DATETIME_FORMAT),
            )._exec(now)
            self.queuejob_uuid = job.uuid
        else:
            self._exec(now)

    def _get_identity_key(self):
        appendix = \
            f"branch:{self.branch_id.repo_id.short}-{self.branch_id.name}:"

        if self.identity_key:
            return self.identity_key + " " + appendix
        name = self._get_short_name()
        return f"{self.branch_id.project_name}_{name} " + appendix

    def _get_short_name(self):
        name = self.name or ''
        if name.startswith("_"):
            name = name[1:]
        return name

    @contextmanager
    def _new_cursor(self, new_cursor):
        if new_cursor:
            with closing(self.env.registry.cursor()) as cr:
                env2 = api.Environment(cr, SUPERUSER_ID, {})
                yield env2
        else:
            yield self.env

    def _exec(self, now=False):
        self = self.sudo()
        args = {}
        short_name = self._get_short_name()
        # TODO make testruns not block reloading
        self = self.with_context(active_test=False)
        if not self.branch_id:
            breakpoint()
            raise Exception("Branch not given for task.")
        detailinfo = (
            f"taskid: {self.id} - {self.name} at branch {self.branch_id.name}"
        )
        with pg_advisory_lock(
            self.env.cr, f"task-branch-{self.branch_id.id}",
            detailinfo=detailinfo
        ):
            with self._new_cursor(not now) as env2:
                self = env2[self._name].browse(self.id)
                self.state = 'started'
                self.env.cr.commit()

                self = self.with_env(env2)
                with self.branch_id.shell(short_name) as shell:

                    started = arrow.get()
                    try:
                        self._internal_exec(shell)
                    except RetryableJobError:
                        raise

                    except Exception:
                        self.env.cr.rollback()
                        msg = traceback.format_exc()
                        log = msg + '\n' + '\n'.join(shell.logsio.get_lines())
                        self.state = 'failed'
                        if self.branch_id:
                            self.branch_id.message_post(
                                body=f"Error happened {self.name}\n{msg}")
                    else:
                        self.state = 'done'
                        log = '\n'.join(shell.logsio.get_lines())
                        if self.branch_id:
                            self.branch_id.message_post(
                                body=f"Successfully executed {self.name}")
                    finally:
                        self.env.cr.commit()

                    duration = (arrow.get() - started).total_seconds()
                    if shell.logsio:
                        shell.logsio.info(
                            f"Finished after {duration} seconds!")

                    self.duration = duration
                    self.log = log
                    if args.get("delete_task"):
                        if self.state == 'done':
                            self.unlink()

    @api.model
    def _cron_cleanup(self):
        dt = arrow.get().shift(days=-20).strftime("%Y-%m-%d %H:%M:%S")
        self.search([
            ('create_date', '<', dt)
            ]).unlink()

    def requeue(self):
        for rec in self.filtered(lambda x: x.state in ['failed']):
            rec.queue_job_id.requeue()

    def _compute_queuejob(self):
        for rec in self:
            if rec.queuejob_uuid:
                rec.queue_job_id = self.env['queue.job'].search([
                    ('uuid', '=', rec.queuejob_uuid)], limit=1)
            else:
                rec.queue_job_id = False

    def _set_failed_if_no_queuejob(self):
        breakpoint()
        for task in self:
            if not task.queue_job_id:
                if not task.state or task.state in ['started']:
                    task.state = 'failed'
                continue
            if task.queue_job_id.state in ['failed', 'done']:
                if not task.state or task.state == 'started':
                    task.state = 'failed'

    def _internal_exec(self, shell):
        # functions called often block the repository access
        args = {
            'task': self,
            'logsio': shell.logsio,
            'shell': shell,
            }
        if self.kwargs and self.kwargs != 'null':
            args.update(json.loads(self.kwargs))
        if not args.get('no_repo', False):
            self.branch_id.repo_id._get_main_repo(
                destination_folder=shell.cwd,
                machine=self.machine_id,
                limit_branch=self.branch_id.name,
                )
        obj = self.env[self.model].sudo().browse(self.res_id)
        # mini check if it is a git repository:
        commit = None
        if not args.get('no_repo', False):
            try:
                shell.X(["git", "status"])
            except Exception:
                pass
            else:
                sha = shell.X([
                    "git", "log", "-n1", "--format=%H"])['stdout'].strip()
                commit = self.branch_id.commit_ids.filtered(
                    lambda x: x.name == sha)

        try:
            exec('obj.' + self.name + "(**args)", {
                'obj': obj,
                'args': args
                })
        finally:
            if commit:
                self.sudo().commit_id = commit
                self.env.cr.commit()
