import arrow
from contextlib import contextmanager
import base64
import datetime
from . import pg_advisory_lock
from collections import deque
import traceback
import time
from odoo import _, api, fields, models, SUPERUSER_ID, registry
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.exceptions import ValidationError
import logging
from pathlib import Path

MAX_ERROR_SIZE = 100 * 1024 * 1024 * 1024

logger = logging.getLogger(__name__)


class AbortException(Exception):
    pass


class WrongShaException(Exception):
    pass


class CicdTestRun(models.Model):
    _inherit = ['mail.thread', 'cicd.open.window.mixin']
    _name = 'cicd.test.run'
    _order = 'id desc'

    name = fields.Char(compute="_compute_name")
    do_abort = fields.Boolean("Abort when possible")
    date = fields.Datetime(
        "Date Started", default=lambda self: fields.Datetime.now(),
        required=True, tracking=True)
    commit_id = fields.Many2one("cicd.git.commit", "Commit", required=True)
    commit_id_short = fields.Char(related="commit_id.short", store=True)
    branch_id = fields.Many2one(
        'cicd.git.branch', string="Initiating branch", required=True)
    branch_id_name = fields.Char(related='branch_id.name', store=False)
    branch_ids = fields.Many2many(
        'cicd.git.branch', related="commit_id.branch_ids", string="Branches")
    repo_short = fields.Char(related="branch_ids.repo_id.short")
    state = fields.Selection([
        ('open', 'Ready To Test'),
        ('running', 'Running'),
        ('success', 'Success'),
        ('omitted', 'Omitted'),
        ('failed', 'Failed'),
    ], string="Result", required=True, default='open')
    success_rate = fields.Integer("Success Rate [%]", tracking=True)
    line_ids = fields.One2many('cicd.test.run.line', 'run_id', string="Lines")
    duration = fields.Integer("Duration [s]", tracking=True)

    def abort(self):
        self.do_abort = True
        self.state = 'failed'

    def _wait_for_postgres(self, shell):
        timeout = 60
        started = arrow.get()
        deadline = started.shift(seconds=timeout)

        while True:
            try:
                shell.odoo(
                    "psql",
                    "--non-interactive",
                    "--sql",
                    "select * from information_schema.tables limit 1;",
                    timeout=timeout)
            except Exception:
                diff = arrow.get() - started
                msg = f"Waiting for postgres {diff.total_seconds()}..."
                logger.info(msg)
                if arrow.get() < deadline:
                    time.sleep(0.5)
                else:
                    raise
            else:
                break

    def _reload(self, shell, logsio, settings, report, started, root):
        def reload():
            self.branch_id._reload(
                shell, None, logsio, project_name=shell.project_name,
                settings=settings, commit=self.commit_id.name)
        logsio.info("Reloading for test run")
        try:
            try:
                reload()
            except RetryableJobError:
                raise
            except Exception as ex:
                report(str(ex))
                try:
                    if shell.cwd != root:
                        shell.rm(shell.cwd)
                    reload()
                except RetryableJobError:
                    raise
                except Exception as ex:
                    if 'reference is not a tree' in str(ex):
                        raise RetryableJobError((
                            "Missing commit not arrived "
                            "- retrying later.")) from ex
                    report(
                        "Error occurred", exception=ex,
                        duration=arrow.get() - started)
                    raise
        except RetryableJobError as ex:
            logsio.error(str(ex))
            report("Retrying", exception=ex)
            raise

        except Exception as ex:
            logsio.error(str(ex))
            report("Error", exception=ex)
            raise

    def _abort_if_required(self):
        self.env.cr.commit()
        self.env.clear()
        if self.do_abort:
            raise AbortException("User aborted")

    def _prepare_run(self, shell, report, logsio, started):
        breakpoint()
        self._abort_if_required()
        report('building')
        shell.odoo('build')
        report('killing any existing')
        shell.odoo('kill', allow_error=True)
        shell.odoo('rm', allow_error=True)
        report('starting postgres')
        shell.odoo('up', '-d', 'postgres')

        self._abort_if_required()

        self._wait_for_postgres(shell)
        report('db reset started')
        shell.odoo('-f', 'db', 'reset')
        # required for turn-into-dev
        shell.odoo('update', 'mail')

        self._abort_if_required()
        report('db reset done')

        self._abort_if_required()
        report("Turning into dev db (change password, set mailserver)")
        shell.odoo('turn-into-dev')

        report("Storing snapshot")
        shell.odoo('snap', 'save', shell.project_name, force=True)
        self._wait_for_postgres(shell)
        report("Storing snapshot done")
        logsio.info("Preparation done")
        report(
            'preparation done', ttype='log', state='success',
            duration=(arrow.get() - started).total_seconds()
        )

        self._abort_if_required()

    def _finalize_testruns(self, shell, report, logsio):
        try:
            report('Finalizing Testing')
            shell.odoo('kill', allow_error=True)
            shell.odoo('rm', force=True, allow_error=True)
            shell.odoo('snap', 'clear')
            shell.odoo('down', "-v", force=True, allow_error=True)
            project_dir = shell.cwd
            with shell.clone(cwd=shell.cwd.parent) as shell:
                try:
                    shell.rm(project_dir)
                except Exception:
                    msg = f"Failed to remove directory {project_dir}"
                    if logsio:
                        logsio.error(msg)
                    logger.error(msg)
        finally:
            if logsio:
                logsio.stop_keepalive()

    @contextmanager
    def prepare_run(self, machine, logsio):
        settings = """
RUN_POSTGRES=1
DB_HOST=postgres
DB_PORT=5432
DB_USER=odoo
DB_PWD=odoo
ODOO_DEMO=1
        """

        def report(
            msg, state='success', exception=None, duration=None, ttype='log'
        ):
            if not hasattr(report, 'last_report_time'):
                report.last_report_time = arrow.get()
            if duration is None:
                duration = (arrow.get() - report.last_report_time)\
                    .total_seconds()
            elif isinstance(duration, datetime.timedelta):
                duration = duration.total_seconds()
            ttype = ttype or 'log'
            if exception:
                state = 'failed'
                msg = (msg or '') + '\n' + str(exception)
            else:
                state = state or 'success'

            self.line_ids = [[0, 0, {
                'state': state,
                'name': msg,
                'ttype': 'preparation',
                'duration': duration
                }]]
            self.env.cr.commit()

            if logsio:
                if state == 'success':
                    logsio.info(msg)
                else:
                    logsio.error(msg)
            report.last_report_time = arrow.get()

        root = machine._get_volume('source')
        started = arrow.get()
        self.date = fields.Datetime.now()
        project_name = self.branch_id.project_name
        assert project_name
        with machine._shell(
            cwd=root / project_name, logsio=logsio,
            project_name=project_name
        ) as shell:
            try:
                report("Checking out source code...")
                self._reload(shell, logsio, settings, report, started, root)
                report("Reloaded")

                sha = shell.X(["git", "log", "-n1", "--format=%H"])[
                    'stdout'].strip()
                if sha != self.commit_id.name:
                    raise WrongShaException((
                        f"checked-out SHA {sha} "
                        f"not matching test sha {self.commit_id.name}"
                        ))
            except WrongShaException:
                pass

            except Exception as ex:
                report("Error at reloading the instance / getting source")
                report(str(ex))
                logger.error(ex)
                raise

            report(f"Checked out source code at {shell.cwd}")
            try:
                try:
                    self._prepare_run(shell, report, logsio, started)
                    self.env.cr.commit()
                except RetryableJobError:
                    raise
                except Exception as ex:
                    duration = arrow.get() - started
                    report("Error occurred", exception=ex, duration=duration)
                    raise

                yield shell

            finally:
                self._finalize_testruns(shell, report, logsio)

    def execute_now(self):
        self.with_context(DEBUG_TESTRUN=True, FORCE_TEST_RUN=True).execute()
        return True

    # ----------------------------------------------
    # Entrypoint
    # ----------------------------------------------
    # env['cicd.test.run'].with_context(DEBUG_TESTRUN=True, FORCE_TEST_RUN=True).browse(nr).execute()
    def execute(self, shell=None, task=None, logsio=None):
        breakpoint()
        with self.branch_id._get_new_logsio_instance(
                'test-run-execute') as logsio2:
            if not logsio:
                logsio = logsio2
            testrun_context = f"_testrun_{self.id}"

            # after logsio, so that logs io projectname is unchanged
            self = self.with_context(testrun=testrun_context)
            with pg_advisory_lock(
                self.env.cr,
                f"testrun.{self.id}", detailinfo="execute test"
            ):
                try:
                    if self.state not in ('open') and not self.env.context.get(
                            "FORCE_TEST_RUN"):
                        return
                    db_registry = registry(self.env.cr.dbname)
                    with db_registry.cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        self = env[self._name].browse(self.id)
                        self = self.with_context(testrun=testrun_context)

                        self.ensure_one()
                        b = self.branch_id
                        started = arrow.get()

                        if not b.any_testing:
                            self.success_rate = 100
                            self.state = 'success'
                            b._compute_state()
                            return

                        self.line_ids = [[6, 0, []]]
                        self.line_ids = [[0, 0, {
                            'run_id': self.id,
                            'ttype': 'log',
                            'name': 'Started'
                            }]]
                        self.do_abort = False
                        self.state = 'running'
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

                        with self.prepare_run(machine, logsio) as shell:
                            if b.run_unittests:
                                self._execute(
                                    shell, logsio, self._run_unit_tests,
                                    machine, 'test-units')
                                self.env.cr.commit()
                            if b.run_robottests:
                                self._execute(
                                    shell, logsio, self._run_robot_tests,
                                    machine, 'test-robot')
                                self.env.cr.commit()
                            if b.simulate_install_id:
                                self._execute(
                                    shell, logsio, self._run_update_db,
                                    machine, 'test-migration')
                                self.env.cr.commit()

                        if data['technical_errors']:
                            for error in data['technical_errors']:
                                if len(error) > MAX_ERROR_SIZE:
                                    error = error[-MAX_ERROR_SIZE:]

                                data['run_lines'].append({
                                    'exc_info': error,
                                    'ttype': 'log',
                                    'state': 'failed',
                                })
                            raise Exception('\n\n\n'.join(map(
                                str, data['technical_errors'])))

                        self.duration = (arrow.get() - started).total_seconds()
                        if logsio:
                            logsio.info(f"Duration was {self.duration}")
                        self._compute_success_rate()
                        self._inform_developer()
                        self.env.cr.commit()
                except Exception as ex:
                    try:
                        self.env.cr.commit()
                    except Exception:
                        pass

                    logger.error(str(ex))
                    try:
                        logsio.error(str(ex))
                    except Exception:
                        pass

                    with db_registry.cursor() as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        testrun = env[self._name].browse(self.id)
                        testrun.state = 'failed'
                        testrun.branch_id._compute_state()
                        cr.commit()
                    raise

    def _execute(self, shell, logsio, run, machine, appendix):
        try:
            logsio.info("Running " + appendix)
            passed_prepare = False
            try:
                started = arrow.get()
                run(shell, logsio)
            except Exception:
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
            lines = rec.mapped('line_ids').filtered(
                lambda x: x.ttype != 'log')
            success_lines = len(lines.filtered(
                lambda x: x.state == 'success' or x.force_success))
            if lines and all(
                    x.state == 'success' or x.force_success for x in lines):
                rec.state = 'success'
            else:
                rec.state = 'failed'
            if not lines or not success_lines:
                rec.success_rate = 0
            else:
                rec.success_rate = \
                    int(100 / float(len(lines)) * float(success_lines))
            rec.branch_id._compute_state()

    @api.constrains('branch_ids')
    def _check_branches(self):
        for rec in self:
            if not rec.branch_ids:
                continue
            if not all(
                x.repo_id == rec.branch_ids[0].repo_id for x in rec.branch_ids
            ):
                raise ValidationError(
                    "Branches must be of the same repository.")

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
            raise ValidationError(
                _("State of branch does not allow a repeated test run"))
        self = self.sudo()
        self.state = 'open' # regular cronjob makes task for that
        self.success_rate = 0

    def _run_create_empty_db(self, shell, task, logsio):

        def _emptydb(item):
            self.branch_id._create_empty_db(shell, task, logsio)

        self._generic_run(
            shell, logsio, [None],
            'emptydb', _emptydb,
        )

    def _run_update_db(self, shell, logsio, **kwargs):

        def _update(item):
            logsio.info(f"Restoring {self.branch_id.dump_id.name}")

            shell.odoo('-f', 'restore', 'odoo-db', self.branch_id.dump_id.name)
            self._wait_for_postgres(shell)
            shell.odoo('update', timeout=self.timeout_migration)
            self._wait_for_postgres(shell)

        self._generic_run(
            shell, logsio, [None],
            'migration', _update,
        )

    def _run_robot_tests(self, shell, logsio, **kwargs):
        files = shell.odoo('list-robot-test-files')['stdout'].strip()
        files = list(filter(bool, files.split("!!!")[1].split("\n")))

        shell.odoo('build')
        configuration = shell.odoo('config', '--full')['stdout'].splitlines()
        host_run_dir = [x for x in configuration if 'HOST_RUN_DIR:' in x]
        host_run_dir = Path(host_run_dir[0].split(":")[1].strip())
        robot_out = host_run_dir / 'odoo_outdir' / 'robot_output'

        def _run_robot_run(item):
            shell.odoo("snap", "restore", shell.project_name)

            self._wait_for_postgres(shell)
            logsio.info('update started')
            shell.odoo('update')

            self._wait_for_postgres(shell)
            shell.odoo('up', '-d')
            self._wait_for_postgres(shell)

            try:
                shell.odoo('robot', item, timeout=self.branch_id.timeout_tests)
                state = 'success'
            except Exception:
                state = 'failed'
            robot_results_tar = shell.grab_folder_as_tar(robot_out)
            robot_results_tar = base64.encodestring(robot_results_tar)
            return {
                'robot_output': robot_results_tar,
                'state': state,
            }

        self._generic_run(
            shell, logsio, files,
            'robottest', _run_robot_run,
        )

    def _run_unit_tests(self, shell, logsio, **kwargs):
        cmd = ['list-unit-test-files']
        if self.branch_id.unittest_all:
            cmd += ['--all']
        files = shell.odoo(*cmd)['stdout'].strip()
        files = list(filter(bool, files.split("!!!")[1].split("\n")))

        tests_by_module = self._get_unit_tests_by_modules(files)
        i = 0
        for module, tests in tests_by_module.items():
            i += 1
            shell.odoo("snap", "restore", shell.project_name)
            self._wait_for_postgres(shell)

            def _update(item):
                shell.odoo('update', item)

            success = self._generic_run(
                shell, logsio, [module],
                'unittest', _update,
                name_prefix='install '
            )
            if not success:
                continue

            self._wait_for_postgres(shell)

            def _unittest(item):
                shell.odoo(
                    'unittest', item, "--non-interactive",
                    timeout=self.branch_id.timeout_tests)

            self._generic_run(
                shell, logsio, tests,
                'unittest', _unittest,
                try_count=self.branch_id.retry_unit_tests,
                name_prefix=f"({i} / {len(tests_by_module)}) {module} "
            )

    def _get_unit_tests_by_modules(self, files):
        tests_by_module = {}
        for fpath in files:
            f = Path(fpath)
            module = str(f.parent.parent.name)
            tests_by_module.setdefault(module, [])
            if fpath not in tests_by_module[module]:
                tests_by_module[module].append(fpath)
        return tests_by_module

    def _generic_run(
        self, shell, logsio, todo, ttype, execute_run,
        try_count=1, name_prefix=''
    ):
        """
        Timeout in seconds.

        """
        success = True
        len_todo = len(todo)
        for i, item in enumerate(todo):
            trycounter = 0
            while trycounter < try_count:
                if self.do_abort:
                    raise AbortException("Aborted by user")
                trycounter += 1
                logsio.info(f"Try #{trycounter}")

                name = name_prefix or ''
                if len_todo > 1:
                    name += f"({i + 1} / {len_todo}) "

                name += (item or '')
                started = arrow.get()
                data = {
                    'name': name,
                    'ttype': ttype,
                    'run_id': self.id,
                    'started': started.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    'try_count': trycounter,
                }
                try:
                    logsio.info(f"Running {name}")
                    result = execute_run(item)
                    if result:
                        data.update(result)

                except Exception:
                    msg = traceback.format_exc()
                    logsio.error(f"Error happened: {msg}")
                    data['state'] = 'failed'
                    data['exc_info'] = msg
                    success = False
                else:
                    # e.g. robottests return state from run
                    if 'state' not in data:
                        data['state'] = 'success'
                end = arrow.get()
                data['duration'] = (end - started).total_seconds()
                if data['state'] == 'success':
                    break

            self.line_ids = [[0, 0, data]]
            self.env.cr.commit()
        return success

    def _inform_developer(self):
        for rec in self:
            partners = (
                rec.commit_id.author_user_ids.mapped('partner_id')
                | rec.mapped('message_follower_ids.partner_id')
                | rec.branch_id.mapped('message_follower_ids.partner_id')
            )

            rec.message_post_with_view(
                "cicd.mail_testrun_result",
                subtype_id=self.env.ref('mail.mt_note').id,
                partner_ids=partners.ids,
                values={
                    "obj": rec,
                },
            )


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
    started = fields.Datetime(
        "Started", default=lambda self: fields.Datetime.now())
    try_count = fields.Integer("Try Count")
    robot_output = fields.Binary("Robot Output", attachment=True)

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