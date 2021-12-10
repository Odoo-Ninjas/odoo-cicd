import base64
import arrow
from copy import deepcopy
import os
import pwd
import grp
import hashlib
from pathlib import Path
from ..tools.logsio_writer import LogsIOWriter
import spur
import spurplus
from contextlib import contextmanager
from odoo import _, api, fields, models, SUPERUSER_ID, tools
import subprocess
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.tools import tempdir
from ..tools.tools import get_host_ip
import logging
logger = logging.getLogger(__name__)

class ShellExecutor(object):
    def __init__(self, machine, cwd, logsio, project_name=None, env={}):
        self.machine = machine
        self.cwd = cwd
        self.logsio = logsio
        self.env = env
        self.project_name = project_name
        if machine:
            assert machine._name == 'cicd.machine'
        if logsio:
            assert isinstance(logsio, LogsIOWriter)
        if project_name:
            assert isinstance(project_name, str)
        if env:
            assert isinstance(env, dict)

    def rmifexists(self, path):
        with self.shell() as spurplus:
            path = str(path)
            if spurplus.exists(path):
                spurplus.run(["rm", "-Rf", path])

    def _get_home_dir(self):
        res = self.machine._execute_shell(
            ['realpath', '~'],
        ).output.strip()
        if res.endswith("/~"):
            res = res[:-2]
        return res

    @contextmanager
    def shell(self):
        with self.machine._shell() as shell:
            yield shell

    def odoo(self, *cmd, allow_error=True):
        env={
            'NO_PROXY': "*",
            'DOCKER_CLIENT_TIMEOUT': "600",
            'COMPOSE_HTTP_TIMEOUT': "600",
            'PSYCOPG_TIMEOUT': "120",
        },
        if not self.project_name:
            raise Exception("Requires project_name for odoo execution")
        cmd = ["odoo", "--project-name", self.project_name] + list(cmd)
        res = self.X(cmd, allow_error=allow_error, env=env)
        if res.return_code and not allow_error:
            if '.FileNotFoundError: [Errno 2] No such file or directory:' in res.stderr_output:
                raise Exception("Seems that a reload of the instance is required.")
            else:
                raise Exception(res.stderr_output)
        return res

    def X(self, cmd, allow_error=False, env=None):
        effective_env = deepcopy(self.env)
        if env:
            effective_env.update(env)
        return self.machine._execute_shell(
            cmd, cwd=self.cwd, env=effective_env, logsio=self.logsio,
            allow_error=allow_error,
        )

class CicdMachine(models.Model):
    _inherit = 'mail.thread'
    _name = 'cicd.machine'

    name = fields.Char("Name")
    is_docker_host = fields.Boolean("Is Docker Host", default=True)
    host = fields.Char("Host")
    volume_ids = fields.One2many("cicd.machine.volume", 'machine_id', string="Volumes")
    ssh_user = fields.Char("SSH User")
    ssh_pubkey = fields.Text("SSH Pubkey", readonly=True)
    ssh_key = fields.Text("SSH Key")
    dump_ids = fields.One2many('cicd.dump', 'machine_id', string="Dumps")
    effective_host = fields.Char(compute="_compute_effective_host", store=False)
    workspace = fields.Char("Workspace", compute="_compute_workspace")
    ttype = fields.Selection([
        ('dev', 'Development-Machine'),
        ('prod', 'Production System'),
    ], required=True)
    reload_config = fields.Text("Settings")
    external_url = fields.Char("External http-Address")

    ssh_user_cicdlogin = fields.Char(compute="_compute_ssh_user_cicd_login")
    ssh_user_cicdlogin_password_salt = fields.Char(compute="_compute_ssh_user_cicd_login", store=True)
    ssh_user_cicdlogin_password = fields.Char(compute="_compute_ssh_user_cicd_login")
    postgres_server_id = fields.Many2one('cicd.postgres', string="Postgres Server")
    upload_dump = fields.Binary("Upload Dump")
    upload_dump_filename = fields.Char("Filename")
    upload_overwrite = fields.Boolean("Overwrite existing")
    upload_volume_id = fields.Many2one('cicd.machine.volume', "Upload Volume", domain=[('ttype', '=', 'dumps')])

    @api.depends('ssh_user')
    def _compute_ssh_user_cicd_login(self):
        for rec in self:
            rec.ssh_user_cicdlogin = self.ssh_user + "_restricted_cicdlogin"
            if not rec.ssh_user_cicdlogin_password_salt:
                rec.ssh_user_cicdlogin_password_salt = str(arrow.get())
            ho = hashlib.md5((rec.ssh_user_cicdlogin + self.ssh_user_cicdlogin_password_salt).encode('utf-8'))
            rec.ssh_user_cicdlogin_password = ho.hexdigest()

    def _compute_workspace(self):
        for rec in self:
            rec.workspace = rec.volume_ids.filtered(lambda x: x.ttype == 'source').name

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)
        return res

    def _compute_effective_host(self):
        for rec in self:
            if rec.is_docker_host:
                rec.effective_host = get_host_ip()
            else:
                rec.effective_host = rec.host

    def _place_ssh_credentials(self):
        self.ensure_one()
        # place private keyfile
        ssh_dir = Path(os.path.expanduser("~/.ssh"))
        ssh_dir.mkdir(exist_ok=True)
        os.chown(ssh_dir, pwd.getpwnam('odoo').pw_uid, grp.getgrnam('odoo').gr_gid)
        os.chmod(ssh_dir, 0o700)

        ssh_keyfile = ssh_dir / self.effective_host
        rights_keyfile = 0o600
        if ssh_keyfile.exists():
            os.chmod(ssh_keyfile, rights_keyfile)
        ssh_keyfile.write_text(self.ssh_key)
        os.chmod(ssh_keyfile, rights_keyfile)
        return ssh_keyfile

    @contextmanager
    def _shell(self):
        self.ensure_one()
        ssh_keyfile = self._place_ssh_credentials()
        with spurplus.connect_with_retries(
            hostname=get_host_ip(),
            username=self.ssh_user,
            private_key_file=str(ssh_keyfile),
            missing_host_key=spur.ssh.MissingHostKey.accept,
            ) as shell:
            yield shell

    @contextmanager
    def _shellexec(self, cwd, logsio, project_name=None, env=None):
        self.ensure_one()
        executor = ShellExecutor(self, cwd, logsio, project_name, env or {})
        yield executor

    def generate_ssh_key(self):
        self.ensure_one()
        with tempdir() as dir:
            subprocess.check_call([
                '/usr/bin/ssh-keygen', '-f', 'temp',
                '-P', ''
            ], cwd=dir)
            keyfile = dir / 'temp'
            pubkeyfile = dir / 'temp.pub'
            self.ssh_key = keyfile.read_text()
            self.ssh_pubkey = pubkeyfile.read_text()

    def test_ssh(self):
        self._execute_shell(["ls"])
        raise ValidationError(_("Everyhing Works!"))

    def _execute_shell(self, cmd, cwd=None, env=None, logsio=None, allow_error=False):

        def convert(x):
            if isinstance(x, Path):
                x = str(x)
            return x

        cmd = list(map(convert, cmd))

        class MyWriter(object):
            def __init__(self, ttype):
                self.text = [""]
                self.ttype = ttype
                self.line = ""

            def finish(self):
                self._write_line()

            def write(self, text):
                if not logsio:
                    return
                if '\n' in text and len(text) == 1:
                    self._write_line()
                    self.line = ""
                else:
                    self.line += text
                    return

            def _write_line(self):
                if not self.line:
                    return
                if self.ttype == 'error':
                    logsio.error(self.line)
                else:
                    logsio.info(self.line)

        with self._shell() as shell:
            stdwriter, errwriter = MyWriter('info'), MyWriter('error')

            res = shell.run(
                cmd, cwd=cwd, update_env=env or {},
                stdout=stdwriter, stderr=errwriter, allow_error=allow_error,
            )
            stdwriter.finish()
            errwriter.finish()
            return res

    def update_dumps(self):
        for rec in self:
            rec.env['cicd.dump']._update_dumps(rec)

    def update_volumes(self):
        self.mapped('volume_ids')._update_sizes()


    def update_all_values(self):
        self.update_dumps()
        self.update_volumes()

    def _get_sshuser_id(self):
        user_name = self.ssh_user
        res = self._execute_shell(self, ["/usr/bin/id", '-u', user_name])
        user_id = res.output.strip()
        return user_id

    def _get_volume(self, ttype):
        res = self.volume_ids.filtered(lambda x: x.ttype == ttype)
        if not res:
            raise ValidationError(_("Could not find: {}").format(ttype))
        return Path(res[0].name)

    def springclean(self, **args):
        """
        Removes all unused source directories, databases
        and does a docker system prune.
        """
        logsio = LogsIOWriter(self.name, 'spring_clean')
        with self._shellexec(cwd="~", logsio=logsio) as shell:
            shell.X(["/usr/bin/docker", "system", "prune", "-f"])

    def make_login_possible_for_webssh_container(self):
        pubkey = Path("/opt/cicd_sshkey/id_rsa.pub").read_text().strip()
        for rec in self:
            with rec._shell() as shell:

                command_file = '/tmp/commands.cicd'
                homedir = '/home/' + rec.ssh_user_cicdlogin
                test_file_if_required = homedir + '/.setup_login_done'
                user_upper = rec.ssh_user_cicdlogin.upper()

                # allow per sudo execution of just the odoo script
                commands = """
#!/bin/bash

#------------------------------------------------------------------------------
# adding sudoer command for restricted user to odoo framework

tee "/etc/sudoers.d/{rec.ssh_user_cicdlogin}_odoo" <<EOF
Cmnd_Alias ODOO_COMMANDS_{user_upper} = /opt/odoo/odoo *
{rec.ssh_user_cicdlogin} ALL=({rec.ssh_user}) NOPASSWD:SETENV: ODOO_COMMANDS_{user_upper}
EOF

#------------------------------------------------------------------------------
# setting up login to restricted user

grep -q "{rec.ssh_user_cicdlogin}" /etc/passwd || adduser --disabled-password --gecos "" {rec.ssh_user_cicdlogin}
mkdir -p ~/.ssh
chmod 700 ~/.ssh
grep -q "{pubkey}" ~/.ssh/authorized_keys || echo "\n{pubkey}" >> ~/.ssh/authorized_keys
usermod --shell /bin/rbash "{rec.ssh_user_cicdlogin}"

#------------------------------------------------------------------------------
# adding programs to restricted user

mkdir -p "{homedir}/programs"
echo 'readonly PATH={homedir}/programs' > "{homedir}/.bash_profile"
echo 'export PATH' >> "{homedir}/.bash_profile"
chown -R "{rec.ssh_user_cicdlogin}":"{rec.ssh_user_cicdlogin}" "{homedir}"
ln -sf /usr/bin/sudo "{homedir}/programs/sudo"

echo -e "{rec.ssh_user_cicdlogin_password}\n{rec.ssh_user_cicdlogin_password}" | passwd "{rec.ssh_user_cicdlogin}"

#------------------------------------------------------------------------------
# adding wrapper for calling odoo framework in that instance directory
#!/bin/bash
tee "{homedir}/programs/odoo" <<EOF
#!/bin/bash
sudo -u {rec.ssh_user} /opt/odoo/odoo --chdir "\$CICD_WORKSPACE/\$PROJECT_NAME" -p "\$PROJECT_NAME" "\$@"
EOF
chmod a+x "{homedir}/programs/odoo"

#------------------------------------------------------------------------------
# make indication file, that it is setup
echo '1' > '{test_file_if_required}'

#------------------------------------------------------------------------------
# self destruct
rm {command_file}

#------------------------------------------------------------------------------
# give calming success message to admin
echo "------------------------------------------------------------------------------------"
echo ""
echo "Successfully allowing restricted bash access from docker container to only execute odoo framework."
echo "Care is taken, that system cannot be compromised."
echo ""
echo "------------------------------------------------------------------------------------"

                """.format(**locals())
                # in this path there ar, the keys that are used by web ssh container /opt/cicd_sshkey
                if not shell.exists(test_file_if_required):
                    shell.write_text(command_file, commands.strip() + "\n")
                    cmd = ["sudo", "/bin/bash", command_file]
                    res = shell.run(cmd, allow_error=True)
                    if res.return_code:
                        raise UserError(f"Failed to setup restrict login. Please execute on host:\n{' '.join(cmd)}\n\nException:\n{res.stderr_output}")

    @tools.ormcache()
    def testoutput(self):
        print('test')

    def write(self, vals):
        if vals.get('upload_dump'):
            self._upload(vals)
        res = super().write(vals)

        # at create if somebody uploaded....mmhh :)
        for rec in self:
            if rec.upload_dump:
                rec.upload_dump = False
        return res

    def upload(self):
        pass

    def _upload(self, vals):
        content = vals.pop('upload_dump')
        filename = vals.pop('upload_dump_filename')

        if not vals.get('upload_volume_id'):
            vols = self.volume_ids.filtered(lambda x: x.ttype == 'dumps')
            if len(vols) > 1:
                raise ValidationError("Please choose a volume!")
            vol = vols[0]
            del vols
        else:
            vol = self.volume_ids.browse(vals['upload_volume_id'])

        with self._shellexec(cwd='~', logsio=None) as shell1:
            with shell1.shell() as shell2:
                path = Path(vol.name) / filename
                content = base64.b64decode(content)
                shell2.write_bytes(path, content)
        self.message_post(body="New dump uploaded: " + filename)

        for f in ['upload_volume_id', 'upload_overwrite']:
            if f in vals:
                vals.pop(f)