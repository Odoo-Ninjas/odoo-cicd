import os
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.tools import _get_shell_url

class Branch(models.Model):
    _inherit = 'cicd.git.branch'

    def update_all_modules(self):
        self._make_task("_update_all_modules")

    def update_installed_modules(self):
        self._make_task("_update_installed_modules")

    def build(self):
        self._make_task("_build")

    def reset_db(self):
        self._make_task("_reset_db")

    def anonymize(self):
        self._make_task("_anonymize")

    def shrinkdb(self):
        self._make_task("_shrink_db")

    def docker_start(self):
        self._make_task("_docker_start", no_repo=True)

    def docker_remove(self):
        self._make_task("_docker_remove", no_repo=True)

    def docker_stop(self):
        self._make_task("_docker_stop")

    def create_empty_db(self):
        self._make_task("_create_empty_db")

    def _check_dump_requirements(self):
        if not self.backup_machine_id:
            raise ValidationError(_("Please choose a machine where dump/restoring happens."))

    def dump(self):
        self._check_dump_requirements()
        self._make_task("_dump", machine=self.backup_machine_id)

    def turn_into_dev(self):
        self._make_task("_turn_into_dev")

    def reload(self):
        self._make_task("_reload")

    def reload_and_restart(self):
        self._make_task("_reload_and_restart")

    def remove_web_assets(self):
        self._make_task("_remove_web_assets")

    def backup(self):
        return {
            'view_type': 'form',
            'res_model': 'cicd.wiz.dump',
            'context': {
                'default_ttype': 'backup',
                'default_branch_id': self.id,
                'default_machine_id': self.repo_id.machine_id.id,
            },
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'flags': {'form': {
            }},
            'target': 'new',
        }

    def restore_dump(self):
        return {
            'view_type': 'form',
            'res_model': 'cicd.wiz.dump',
            'context': {
                'default_ttype': 'restore',
                'default_branch_id': self.id,
                'default_machine_id': self.repo_id.machine_id.id,
            },
            'views': [(False, 'form')],
            'type': 'ir.actions.act_window',
            'flags': {'form': {
            }},
            'target': 'new',
        }

    def run_tests(self):
        if not self.latest_commit_id:
            raise ValidationError((
                "Missing latest commit."
                f" {self.name}"
            ))
        self.with_delay(identity_key=(
            f"{self.latest_commit_id.name}-run-tests"
        ))._run_tests()

    def start(self):
        return {
            'type': 'ir.actions.act_url',
            #Hotfix
            #'url': '/start/' + self.project_name,
            'url': '/start/' + self.name,
            'target': 'self'
        }

    def update_git_commits(self):
        self._make_task("_update_git_commits", no_repo=True)

    def transform_input_dump(self):
        self._make_task("_transform_input_dump")

    def start_webmailer(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/start/' + self.project_name + "/mailer/startup",
            'target': 'self'
        }

    def _shell_url(self, cmd, machine=None, tmux=None):
        """
        tmux: -A create or append, -s name of session"
        """
        if tmux:
            # tmux: -A create or append, -s name of session"
            cmd = [
                "tmux", "new-session", "-A",
                "-s", f"{self.project_name}_shell",
            ] + cmd

        machine = self.machine_id
        machine.make_login_possible_for_webssh_container()
        path = machine._get_volume('source')
        path = path / self.project_name
        shell_url = _get_shell_url(
            machine.effective_host,
            machine.ssh_user_cicdlogin,
            machine.ssh_user_cicdlogin_password,
            [
                f"export CICD_WORKSPACE={self.machine_id._get_volume('source')};",
                f"export PROJECT_NAME={self.project_name};",
            ] + cmd
        )
        return {
            'type': 'ir.actions.act_url',
            'url': shell_url,
            'target': 'new'
        }

    def pgcli(self):
        return self._shell_url(["odoo", "pgcli"], tmux='pgcli')

    def open_odoo_shell(self):
        return self._shell_url(["odoo", "shell"], tmux='odoo_shell')

    def debug_webcontainer(self):
        return self._shell_url(["odoo", "debug", "odoo"], tmux='debug_odoo')

    def open_shell(self):
        return self._shell_url([], tmux='_shell')

    def start_logs(self):
        with self.shell('show_logs') as shell:
            stdout = shell.odoo("config", "--full")['stdout']
            queuejobs = 'RUN_ODOO_QUEUEJOBS=1' in stdout
            cronjobs = 'RUN_ODOO_CRONJOBS=1' in stdout
        containers = ['odoo']
        if queuejobs:
            containers += ['odoo_queuejobs']
        if cronjobs:
            containers += ['odoo_cronjobs']
        return self._shell_url(["odoo", "logs", "-f"] + containers)

    def refresh_tasks(self):
        return True

    def checkout_latest(self):
        self._make_task("_checkout_latest", now=False)

    def garbage_collect(self):
        self._make_task("_gc")

    def fetch(self):
        self.repo_id._fetch_branch(self.name)
