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
    dump_used = fields.Char("Dump used", readonly=True)
    duration = fields.Integer("Duration [s]", readonly=True)
    commit_id = fields.Many2one(
        "cicd.git.commit", string="Commit", readonly=True)
    testrun_id = fields.Many2one('cicd.test.run')

    kwargs = fields.Text("KWargs")
    identity_key = fields.Char()
    started = fields.Datetime("Started")

    def _get_queuejob(self):
        self.ensure_one()
        return self.env['queue.job'].sudo().search([(
                'identity_key', '=', self.qj_identity_key)], limit=1)

    def _compute_state(self):
        for rec in self:
            qj = rec._get_queuejob()
            if not qj:
                # keep last state as queuejobs are deleted from time to time
                pass
            else:
                rec.state = qj.state
                rec.log = qj.exc_info

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

    @property
    def qj_identity_key(self):
        return (
            "execute-task-"
            f"{self.id}"
        )

    def _exec(self, now=False):
        if not self.branch_id:
            raise Exception("Branch not given for task.")

        if not now:
            self.env.cr.execute((
                "select count(*) "
                "from queue_job "
                "where identity_key = %s"
            ), tuple([self.qj_identity_key]))
            count_jobs = self.env.cr.fetchone()[0]
            if count_jobs:
                return

        self.state = 'started'
        self.started = fields.Datetime.now()

        self.custom_with_delay(
            delayed=not now,
            identity_key=self.qj_identity_key,
        )._internal_exec(now)

    @api.model
    def _cron_cleanup(self):
        dt = arrow.get().shift(days=-20).strftime("%Y-%m-%d %H:%M:%S")
        self.search([
            ('create_date', '<', dt)
            ]).unlink()

    def requeue(self):
        for rec in self.filtered(lambda x: x.state in ['failed']):
            qj = rec._get_queuejob()
            if qj and qj.state in ['done', 'failed']:
                qj.requeue()
            else:
                rec._exec(now=False)

    def _set_failed_if_no_queuejob(self):
        for task in self:
            task._compute_state()
            if task.state == 'started':
                qj = task._get_queuejob()
                if not qj or qj.state in ['done', 'failed']:
                    task.state = 'failed'

    def _get_args(self, shell):
        self.ensure_one()
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
        self.env['base'].flush()
        self.env.cr.commit()
        return args

    def _internal_exec(self, now=False, delete_after=False):
        # functions called often block the repository access
        args = {}
        log = None
        commit = None

        try:
            self = self.sudo().with_context(active_test=False)
            short_name = self._get_short_name()
            with self.branch_id.shell(short_name) as shell:
                self.env['base'].flush()
                self.env.cr.commit()
                args = self._get_args(shell)
                delete_after = args.get('delete_task')
                obj = self.env[self.model].sudo().browse(self.res_id)
                if self.res_id and not obj.exists():
                    raise Exception((
                        f"Not found: {self.res_id} {self.model}"
                    ))


                # mini check if it is a git repository:
                if not args.get('no_repo', False):
                    try:
                        shell.X(["git", "status"])
                    except Exception:
                        pass
                    else:
                        sha = shell.X([
                            "git", "log", "-n1",
                            "--format=%H"])['stdout'].strip()
                        commit = self.branch_id.commit_ids.filtered(
                            lambda x: x.name == sha)

                exec('obj.' + self.name + "(**args)", {
                    'obj': obj,
                    'args': args
                    })
                if shell.logsio:
                    shell.logsio.info(f"Finished!")

        except Exception:
            self.env.cr.rollback()
            self.env.clear()
            log = traceback.format_exc() + \
                '\n' + '\n'.join(shell.logsio.get_lines())
            state = 'failed'
        else:
            state = 'done'
            log = '\n'.join(shell.logsio.get_lines())

        duration = 0
        if self.started:
            duration = (arrow.utcnow() - arrow.get(self.started)) \
                .total_seconds()
        self.custom_with_delay(
            delayed=not now,
            identity_key=self.qj_identity_key + "-finish"
        )._finish_task(
            state=state,
            duration=duration,
            delete_after=delete_after,
            log=log,
            commit_id=commit and commit.id or False,
        )

    def _finish_task(self, state, duration, delete_after, log, commit_id):

        if delete_after and state == 'done':
            self.unlink()
            return

        self.write({
            'state': state,
            'log': log,
            'duration': duration
        })
        if self.branch_id:
            if state == 'failed':
                self.branch_id.message_post(
                    body=f"Error happened {self.name}\n{log[-250:]}")
            elif state == 'done':
                self.branch_id.message_post(
                    body=f"Successfully executed {self.name}")
        self.env['base'].flush()
        self.env.cr.commit()

    def custom_with_delay(self, delayed, identity_key):
        if delayed:
            return self.with_delay(
                identity_key=identity_key
            )
        else:
            return self
