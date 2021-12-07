import json
import re
import arrow
import os
import requests
from odoo import registry
from pathlib import Path
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from ..tools.logsio_writer import LogsIOWriter
from contextlib import contextmanager
import humanize

class GitBranch(models.Model):
    _inherit = ['mail.thread']
    _name = 'cicd.git.branch'

    project_name = fields.Char(compute="_compute_project_name", store=False)
    approver_ids = fields.Many2many("res.users", "cicd_git_branch_approver_rel", "branch_id", "user_id", string="Approver")
    machine_id = fields.Many2one(related='repo_id.machine_id')
    backup_machine_id = fields.Many2one('cicd.machine', string="Machine for backup/restore")
    backup_filename = fields.Char("Backup Filename")
    last_access = fields.Datetime("Last Access")
    cycle_down_after_seconds = fields.Integer("Cycle Down After Seconds", default=3600)
    name = fields.Char("Git Branch", required=True)
    date_registered = fields.Datetime("Date registered")
    date = fields.Datetime("Date")
    repo_id = fields.Many2one('cicd.git.repo', string="Repository", required=True)
    repo_short = fields.Char(related="repo_id.short")
    active = fields.Boolean("Active", default=True)
    commit_ids = fields.Many2many('cicd.git.commit', string="Commits")
    ticket_system_url = fields.Char(compute="_compute_ticket_system_url")
    task_ids = fields.One2many('cicd.task', 'branch_id', string="Tasks")
    docker_state = fields.Char("Docker State", readonly=True, compute="_compute_docker_state")
    state = fields.Selection([
        ('new', 'New'),
        ('dev', "Dev"),
        ('approve', "Approve"),
        ('testable', 'Testable'), 
        ('tested', 'Tested'),
        ('blocked', "Blocked"),
        ('candidate', 'Candidate'),
        ('release', 'Release'),
        ('done', "Done"),
        ('cancel', "Cancel"),
    ], string="State", default="new", track_visibility='onchange', compute="_compute_state", store=True)
    build_state = fields.Selection([
        ('new', 'New'),
        ('fail', 'Failed'),
        ('done', 'Done'),
        ('building', 'Building'),
    ], default="new", required=True, compute="_compute_build_state", string="Instance State")
    dump_id = fields.Many2one("cicd.dump", string="Dump")
    db_size = fields.Integer("DB Size Bytes")
    db_size_humanize = fields.Char("DB Size", compute="_compute_human")
    reload_config = fields.Text("Reload Config")
    autobackup = fields.Boolean("Autobackup") # TODO implement
    enduser_summary = fields.Text("Enduser Summary")
    release_ids = fields.One2many("cicd.release", "branch_id", string="Releases")
    release_item_ids = fields.Many2many('cicd.release.item', "Releases", compute="_compute_releases")

    any_testing = fields.Boolean(compute="_compute_any_testing")
    run_unittests = fields.Boolean("Run Unittests", default=True, testrun_field=True)
    run_robottests = fields.Boolean("Run Robot-Tests", default=True, testrun_field=True)
    simulate_empty_install = fields.Boolean("Simulate Empty Install", default=True, testrun_field=True)
    simulate_install_id = fields.Many2one("cicd.dump", string="Simulate Install", testrun_field=True)

    test_run_ids = fields.One2many('cicd.test.run', string="Test Runs", compute="_compute_test_runs")
    block_release = fields.Boolean("Block Release")
    container_ids = fields.One2many('docker.container', 'branch_id', string="Containers")

    _sql_constraints = [
        ('name_repo_id_unique', "unique(name, repo_id)", _("Only one unique entry allowed.")),
    ]

    def _compute_any_testing(self):
        for rec in self:
            fields = [k for k, v in rec._fields.items() if getattr(v, 'testrun_field', False)]
            rec.any_testing = any(rec[f] for f in fields)

    @api.model
    def create(self, vals):
        res = super().create(vals)
        if not res.simulate_install_id:
            res.simulate_install_id = res.repo_id.default_simulate_install_id_dump_id
        return res

    def _compute_releases(self):
        for rec in self:
            release_items = self.env['cicd.release.item'].search([
                ('commit_ids', 'in', rec.commit_ids.ids)
            ])
            rec.release_item_ids = release_items.ids

    def approve(self):
        self.approver_ids = [[0, 0, {
            'user_id': self.env.user.id,
            'commit_id': self.commit_ids[0].id,
            'comment': self.approve_message,
            'state': 'ok',
        }]]
        self.approve_message = False
        self.state = 'approved'

    def decline(self):
        self.approver_ids = [[0, 0, {
            'user_id': self.env.user.id,
            'commit_id': self.commit_ids[0].id,
            'comment': self.approve_message,
            'state': 'not ok',
        }]]
        self.approve_message = False
        self.state = 'rework'

    @api.depends("container_ids", "container_ids.state")
    def _compute_docker_state(self):
        for rec in self:
            count_up = len(rec.container_ids.filtered(lambda x: x.state == 'up'))
            count_down = len(rec.container_ids.filtered(lambda x: x.state == 'down'))
            rec.docker_state = f"Up: {count_up} Down: {count_down}"

    def set_state(self, state, raise_exception=False):
        self.ensure_one()
        self.state = state

    @api.depends(
        "commit_ids",
        "commit_ids.approval_state",
        "commit_ids.test_state",
        "commit_ids.force_approved",
        "commit_ids.test_run_ids",
        "commit_ids.test_run_ids.state",
    )
    def _compute_state(self):
        for rec in self:
            rec.state = 'new'
            if not rec.commit_ids and rec.build_state == 'new':
                continue

            commit = rec.commit_ids.sorted(lambda x: x.date, reverse=True)[0]

            if commit.approval_state == 'check':
                rec.state = 'approve'

            elif commit.approval_state == 'approved' and commit.test_state in [False, 'open'] and rec.any_testing and not commit.force_approved:
                rec.state = 'testable'

            elif commit.test_state == 'failed' or commit.approval_state == 'declined':
                rec.state = 'dev'

            elif (commit.test_state == 'success' or not rec.any_testing and commit.test_state in [False, 'open']) and commit.approval_state == 'approved':

                repo = commit.mapped('branch_ids.repo_id')
                releases = repo.release_ids.filtered(lambda x: rec in x.mapped('item_ids.branch_ids'))
                candidates = releases.mapped('candidate_branch_id')
                released_branches = releases.mapped('branch_id')
                import pudb;pudb.set_trace()

                if any(x.contains_branch(commit) for x in released_branches):
                    if releases.is_latest_release_done:
                        rec.state = 'done'
                    else:
                        rec.state = 'release'
                elif any(x.contains_branch(commit) for x in candidates):
                    rec.state = 'candidate'
                elif rec.block_release:
                    rec.state = 'blocked'
                elif (commit.test_state in [False, 'open'] and not rec.any_testing) or commit.force_approved:
                    rec.state = 'tested'

    @api.fieldchange('state')
    def _onchange_state_event(self, changeset):
        for rec in self:
            old_state = changeset['state']['old']
            new_state = changeset['state']['new']
            if new_state == 'tested' or old_state == 'tested':
                self.env['cicd.release.item'].search([
                    ('state', '=', 'new'),
                    ('release_id.repo_id', '=', rec.repo_id.id)
                ])._collect_tested_branches()

    @api.depends("name")
    def _compute_ticket_system_url(self):
        for rec in self:
            url = rec.repo_id.ticket_system_base_url
            regex = rec.repo_id.ticket_system_regex
            name = rec.name or ''
            if regex:
                m = re.match(regex, name)
                name = m.groups() and m.groups()[0] or ''
            rec.ticket_system_url = (url or '') + (rec.name or '')

    def _compute_test_runs(self):
        for rec in self:
            rec.test_run_ids = rec.mapped('commit_ids.test_run_ids')

    @api.depends("db_size")
    def _compute_human(self):
        for rec in self:
            rec.db_size_humanize = humanize.naturalsize(rec.db_size)

    @api.depends('task_ids', 'task_ids.state')
    def _compute_build_state(self):
        for rec in self:
            if 'new' in rec.mapped('task_ids.state'): 
                rec.build_state = 'building'
            else:
                if rec.task_ids and rec.task_ids[0].state == 'fail':
                    rec.build_state = 'failed'
                elif rec.task_ids and rec.task_ids[0].state == 'done':
                    rec.build_state = 'done'
                else:
                    rec.build_state = 'new'

    def _make_task(self, execute, now=False, machine=None, silent=False, kwargs=None):
        if not now and self.task_ids.filtered(lambda x: x.state == 'new' and x.name == execute):
            if silent:
                return
            raise ValidationError(_("Task already exists. Not triggered again."))
        task = self.env['cicd.task'].sudo().create({
            'model': self._name,
            'res_id': self.id,
            'name': execute,
            'branch_id': self.id,
            'machine_id': (machine and machine.id) or self.machine_id.id,
            'kwargs': json.dumps(kwargs),
        })
        task.perform(now=now)
        return True

    @api.model
    def _cron_update_docker_states(self):
        self.search([])._docker_get_state()

    def _cron_execute_task(self):
        self.ensure_one()
        tasks = self.task_ids.filtered(lambda x: x.state == 'new')
        if not tasks:
            return
        tasks = tasks[-1]
        tasks.perform()

    def _get_instance_folder(self, machine):
        return machine._get_volume('source') / self.project_name

    @contextmanager
    def _shellexec(self, task, logsio, cwd=None):
        instance_folder = self._get_instance_folder(task.machine_id)
        with self.machine_id._shellexec(
            cwd=cwd or instance_folder,
            logsio=logsio,
            project_name=self.project_name,
        ) as shell:
            yield shell

    def make_instance_ready_to_login(self):
        def test_request():
            try:
                response = requests.get("http://" + self._get_odoo_proxy_container_name() + "/web/login")
            except requests.exceptions.ConnectionError:
                return False

            return response.status_code == 200

        if not test_request():
            if self.task_ids.filtered(lambda x: not x.is_done):
                raise ValidationError(_("Instance did not respond. Undone task exists. Please retry later!"))

            self._make_task("_reload_and_restart", now=True)
            if not test_request():
                raise ValidationError(_("Instance did not respond. It was tried to start the application but this did not succeed. Please check task logs."))

    def _get_odoo_proxy_container_name(self): 
        return f"{self.project_name}_proxy"

    def _compute_project_name(self):
        for rec in self:
            rec.project_name = os.environ['CICD_PROJECT_NAME'] + "_" + rec.repo_id.short + "_" + rec.name

    def _get_new_logsio_instance(self, source):
        self.ensure_one()
        rolling_file = LogsIOWriter(f"{self.project_name}", source)
        rolling_file.write_text(f"Started: {arrow.get()}")
        return rolling_file

    @api.constrains("backup_filename")
    def _check_backup_filename(self):
        for rec in self:
            if not rec.backup_filename: continue
            if '/' in rec.backup_filename:
                raise ValidationError("No slashes in backup filename allowed!")

    @api.constrains('active')
    def _on_active_change(self):
        for rec in self:
            if not rec.active:
                logsio = LogsIOWriter(self.project_name, 'set_inactive')
                for machine in self.env['cicd.machine'].search([]):
                    path = machine._get_volume('source')
                    # delete instance folder
                    with machine._shellexec(cwd=path, logsio=logsio) as shell:
                        with shell.shell() as spurplus:
                            project_path = path / rec.project_name
                            if spurplus.exists(project_path):
                                spurplus.remove(project_path, recursive=True)

                        try:
                            shell.odoo("kill")
                        except Exception as ex:
                            logsio.error(str(ex))

                        try:
                            shell.odoo("rm")
                        except Exception as ex:
                            logsio.error(str(ex))

                    # delete db
                    db = machine.database_ids.filtered(lambda x: x.name == rec.project_name).delete_db()

    def toggle_active(self):
        for rec in self:
            rec.active = not rec.active

    def toggle_block_release(self):
        for rec in self:
            rec.block_release = not rec.block_release

    def _cron_make_test_runs(self):
        for rec in self:
            rec._make_task("_run_tests", silent=True, kwargs={'update_state': True})

    def _trigger_rebuild_after_fetch(self):
        """
        After new source is fetched then the instance is rebuilt.
        """
        for rec in self:
            rec._make_task("_build", silent=True)