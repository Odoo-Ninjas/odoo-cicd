from contextlib import contextmanager
import psycopg2
from odoo.addons.queue_job.exception import RetryableJobError
import sys
from collections import deque
import traceback
import time
import arrow
from odoo import _, api, fields, models, SUPERUSER_ID, registry
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import logging
import threading
logger = logging.getLogger(__name__)

class CicdTestRun(models.Model):
    _inherit = ['mail.thread']
    _name = 'cicd.test.run'
    _order = 'date desc'

    name = fields.Char(compute="_compute_name")
    date = fields.Datetime("Date", default=lambda self: fields.Datetime.now(), required=True)
    commit_id = fields.Many2one("cicd.git.commit", "Commit", required=True)
    commit_id_short = fields.Char(related="commit_id.short", store=True)
    branch_id = fields.Many2one('cicd.git.branch', string="Initiating branch", required=True)
    branch_id_name = fields.Char(related='branch_id.name', store=False)
    branch_ids = fields.Many2many('cicd.git.branch', related="commit_id.branch_ids", string="Branches")
    repo_short = fields.Char(related="branch_ids.repo_id.short")
    state = fields.Selection([
        ('open', 'Testing'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string="Result", store=True, required=True, default='open')
    success_rate = fields.Integer("Success Rate [%]")
    line_ids = fields.One2many('cicd.test.run.line', 'run_id', string="Lines")
    duration = fields.Integer("Duration [s]")

    def _wait_for_postgres(self, shell):
        timeout = 60
        started = arrow.get()
        deadline = started.shift(seconds=timeout)

        while True:
            try:
                shell.odoo("psql", "--non-interactive", "--sql", "select * from information_schema.tables limit 1;", timeout=timeout)
            except Exception:
                diff = arrow.get() - started
                logger.info(f"Waiting for postgres {diff.total_seconds()}...")
                if arrow.get() < deadline:
                    time.sleep(0.5)
                else:
                    raise
            else:
                break

    @contextmanager
    def prepare_run(self, machine, logsio):
        settings = """
RUN_POSTGRES=1
        """

        def report(msg):
            self.line_ids = [[0, 0, {'state': 'success', 'name': msg, 'ttype': 'log'}]]
            self.env.cr.commit()
            logsio.info(msg)

        root = machine._get_volume('source')
        with machine._shell(cwd=root, logsio=logsio, project_name=self.branch_id.project_name) as shell:
            report("Checking out source code...")
            self.branch_id._reload(shell, None, logsio, project_name=shell.project_name, settings=settings, commit=self.commit_id.name)
            report("Checked out source code")
            shell.cwd = root / shell.project_name
            try:
                report('building')
                shell.odoo('build')
                report('killing any existing')
                shell.odoo('kill', allow_error=True)
                shell.odoo('rm', allow_error=True)
                report('starting postgres')
                shell.odoo('up', '-d', 'postgres')
                self._wait_for_postgres(shell)
                report('db reset started')
                shell.odoo('-f', 'db', 'reset')
                report('db reset done')
                self._wait_for_postgres(shell)
                report('update started')
                shell.odoo('update')
                report('installation of modules done')
                report("Storing snapshot")
                shell.odoo('snap', 'save', shell.project_name, force=True)
                self._wait_for_postgres(shell)
                report("Storing snapshot done")

                yield shell

            finally:
                try:
                    report('Finalizing Testing')
                    shell.odoo('kill', allow_error=True)
                    shell.odoo('rm', force=True, allow_error=True)
                    shell.odoo('snap', 'clear')
                    shell.odoo('down', "-v", force=True, allow_error=True)
                    project_dir = shell.cwd
                    shell.cwd = shell.cwd.parent
                    shell.rmifexists(project_dir)
                finally:
                    if logsio:
                        logsio.stop_keepalive()




    # ----------------------------------------------
    # Entrypoint
    # ----------------------------------------------
    def execute(self, shell=None, task=None, logsio=None):
        db_registry = registry(self.env.cr.dbname)
        with db_registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self = self.with_env(env)

            if self.state not in ('open'):
                return
            self.ensure_one()
            b = self.branch_id
            started = arrow.get()

            try:
                self.env.cr.execute("select id from cicd_test_run where branch_id=%s for update nowait", (self.branch_id.id,))
            except psycopg2.errors.LockNotAvailable:
                raise RetryableJobError(f"Could not work exclusivley on test-run for branch {self.branch_id.name} - retrying in few seconds", ignore_retry=True, seconds=5)
            if not b.any_testing:
                self.success_rate = 100
                self.state = 'success'
                b._compute_state()
                return

            self.state = 'open'

            self.line_ids = [[6, 0, []]]
            self.line_ids = [[0, 0, {'run_id': self.id, 'ttype': 'log', 'name': 'Started'}]]
            self.env.cr.commit()

            if shell:
                machine = shell.machine
            else:
                machine = self.branch_id.repo_id.machine_id

            data = {
                'testrun_id': self.id,
                'machine_id': machine.id,
                'technical_errors': [],
                'run_lines': deque(),
            }

            if b.run_unittests:
                self._execute(self._run_unit_tests, machine, 'test-units')
                self.env.cr.commit()
            if b.run_robottests:
                self._execute(self._run_robot_tests, machine, 'test-robot')
                self.env.cr.commit()
            if b.simulate_install_id:
                self._execute(self._run_update_db, machine, 'test-migration')
                self.env.cr.commit()

            if data['technical_errors']:
                for error in data['technical_errors']:
                    data['run_lines'].append({
                        'exc_info': error,
                        'ttype': 'log',
                        'state': 'failed',
                    })
                raise Exception('\n\n\n'.join(map(str, data['technical_errors'])))

            self.duration = (arrow.get() - started).total_seconds()
            if logsio:
                logsio.info(f"Duration was {self.duration}")
            self._compute_success_rate()
            self._inform_developer()
            self.env.cr.commit()

    def _make_line(self, line):
        self.line_ids.create(line)

    def _execute(self, run, machine, appendix):
        logsio = None
        try:
            testrun = self
            with testrun.branch_id._get_new_logsio_instance(f"{appendix} - testrun") as logsio:
                testrun = testrun.with_context(testrun=f"_testrun_{testrun.id}_{appendix}") # after logsio, so that logs io projectname is unchanged
                logsio.info("Running " + appendix)
                passed_prepare = False
                try:
                    started = arrow.get()
                    with testrun.prepare_run(machine, logsio) as shell:
                        logsio.info("Preparation done " + appendix)
                        self.line_ids = [[0, 0, {
                            'state': 'success', 'ttype': 'log', 'name': f'preparation done: {appendix}',
                            'duration': (arrow.get() - started).total_seconds()
                            }]]
                        self.env.cr.commit()
                        passed_prepare = True
                        run(shell, logsio)
                except Exception as ex:
                    msg = traceback.format_exc()
                    if not passed_prepare:
                        duration = (arrow.get() - started).total_seconds()
                        self.line_ids = [[0, 0, {
                            'duration': duration,
                            'exc_info': msg,
                            'ttype': 'preparation',
                            'name': "Failed at preparation",
                            'state': 'failed',
                        }]]
                        self.env.cr.commit()

        except Exception as ex:
            msg = traceback.format_exc()
            self._log_error(str(ex))
            self._log_error(msg)
            logger.error(ex)
            logger.error(msg)
            if logsio:
                logsio.error(ex)
                logsio.error(msg)

    def _log_error(self, msg):
        self.line_ids = [[0, 0, {
            'ttype': 'failed',
            'name': msg
        }]]
        self.env.cr.commit()

    def _compute_success_rate(self):
        for rec in self:
            lines = rec.mapped('line_ids').filtered(lambda x: x.ttype != 'log')
            success_lines = len(lines.filtered(lambda x: x.state == 'success' or x.force_success))
            if lines and all(x.state == 'success' or x.force_success for x in lines):
                rec.state = 'success'
            else:
                rec.state = 'failed'
            if not lines or not success_lines:
                rec.success_rate = 0
            else:
                rec.success_rate = int(100 / float(len(lines)) * float(success_lines))

    @api.constrains('branch_ids')
    def _check_branches(self):
        for rec in self:
            if not rec.branch_ids:
                continue
            if not all(x.repo_id == rec.branch_ids[0].repo_id for x in rec.branch_ids):
                raise ValidationError("Branches must be of the same repository.")

    def _compute_name(self):
        for rec in self:
            date = rec.create_date.strftime("%Y-%m-%d %H:%M:%S")[:10]
            rec.name = f"{date} - {rec.branch_id.name}"

    @api.model
    def _get_ttypes(self, filtered):
        for x in self._fields['ttype'].selection:
            if filtered:
                if x[0] not in filtered:
                    continue
            yield x[0]

    def rerun(self):
        if self.branch_id.state not in ['testable', 'tested', 'dev']:
            raise ValidationError(_("State of branch does not allow a repeated test run"))
        self = self.sudo()
        self.state = 'open' # regular cronjob makes task for that

    def _run_create_empty_db(self, shell, task, logsio):
        self._generic_run(
            shell, logsio, [None], 
            'emptydb',
            lambda item: self.branch_id._create_empty_db(shell, task, logsio),
        )

    def _run_update_db(self, shell, logsio, **kwargs):

        def _x(item):
            logsio.info(f"Restoring {self.branch_id.dump_id.name}")

            shell.odoo('-f', 'restore', 'odoo-db', self.branch_id.dump_id.name)
            self._wait_for_postgres(shell)
            shell.odoo('update', self.timeout_migration)
            self._wait_for_postgres(shell)

        self._generic_run(
            shell, logsio, [None], 
            'migration', _x,
        )

    def _run_robot_tests(self, shell, logsio, **kwargs):
        files = shell.odoo('list-robot-test-files')['stdout'].strip()
        files = list(filter(bool, files.split("!!!")[1].split("\n")))

        shell.odoo('build')
        def _x(item):
            shell.odoo("snap", "restore", shell.project_name)
            self._wait_for_postgres(shell)
            shell.odoo('robot', item, timeout=self.branch_id.timeout_tests)

        self._generic_run(
            shell, logsio, files, 
            'robottest', _x,
        )

    def _run_unit_tests(self, shell, logsio, **kwargs):
        cmd = ['list-unit-test-files']
        if self.branch_id.unittest_all:
            cmd += ['--all']
        files = shell.odoo(*cmd)['stdout'].strip()
        files = list(filter(bool, files.split("!!!")[1].split("\n")))

        shell.odoo("snap", "restore", shell.project_name)
        self._wait_for_postgres(shell)

        self._generic_run(
            shell, logsio, files, 
            'unittest',
            lambda item: shell.odoo(
                'unittest',
                item,
                timeout=self.branch_id.timeout_tests,
                ),
            try_count=self.branch_id.retry_unit_tests,
        )

    def _generic_run(self, shell, logsio, todo, ttype, execute_run, try_count=1):
        """
        Timeout in seconds.

        """
        for i, item in enumerate(todo):
            trycounter = 0
            while trycounter < try_count:
                trycounter += 1
                logsio.info(f"Try #{trycounter}")

                index = f"({i + 1} / {len(todo)}"
                started = arrow.get()
                data = {
                    'name': f"{index} {item}",
                    'ttype': ttype,
                    'run_id': self.id,
                    'started': started.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    'try_count': trycounter,
                }
                try:
                    logsio.info(f"Running {index} {item}")
                    execute_run(item)

                except Exception:
                    msg = traceback.format_exc()
                    logsio.error(f"Error happened: {msg}")
                    data['state'] = 'failed'
                    data['exc_info'] = msg
                else:
                    data['state'] = 'success'
                end = arrow.get()
                data['duration'] = (end - started).total_seconds()
                if data['state'] == 'success':
                    break

            self.line_ids = [[0, 0, data]]
            self.env.cr.commit()

    def _inform_developer(self):
        for rec in self:
            partners = (
                rec.commit_id.author_user_ids.mapped('partner_id') | rec.mapped('message_follower_ids.partner_id')
            )

            rec.message_post_with_view(
                "cicd.mail_testrun_result",
                subtype_id=self.env.ref('mail.mt_note').id,
                partner_ids=partners.ids,
                values={
                    "obj": rec,
                },
            )

class CicdTestRun(models.Model):
    _name = 'cicd.test.run.line'
    _order = 'started'

    ttype = fields.Selection([
        ('preparation', "Preparation"),
        ('unittest', 'Unit-Test'),
        ('robottest', 'Robot-Test'),
        ('migration', 'Migration'),
        ('emptydb', 'Migration'),
        ('log', "Log-Note"),
    ], string="Category")
    name = fields.Char("Name")
    run_id = fields.Many2one('cicd.test.run', string="Run")
    exc_info = fields.Text("Exception Info")
    duration = fields.Integer("Duration")
    state = fields.Selection([
        ('open', 'Open'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], default='open', required=True)
    force_success = fields.Boolean("Force Success")
    started = fields.Datetime("Started", default=lambda self: fields.Datetime.now())
    try_count = fields.Integer("Try Count")

    def toggle_force_success(self):
        self.sudo().force_success = not self.sudo().force_success

    @api.recordchange('force_success')
    def _onchange_force(self):
        for rec in self:
            rec.run_id._compute_success_rate()

    def open_form(self):
        return {
            'name': self.name,
            'view_type': 'form',
            'res_model': self._name,
            'res_id': self.id,
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'target': 'current',
        }

    @api.model
    def create(self, vals):
        res = super().create(vals)
        res.run_id.state = 'open'
        return res