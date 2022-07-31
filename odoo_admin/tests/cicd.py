from subprocess import check_output, check_call
import tempfile
import json
import yaml
from pathlib import Path
import shutil
import os
import inspect
import os
from pathlib import Path
from robot.libraries.BuiltIn import BuiltIn

current_dir = Path(
    os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
)
rsa_file = current_dir / "res" / "id_rsa"
rsa_file_pub = current_dir / "res" / "id_rsa.pub"


class cicd(object):
    def _get_MANIFEST(self, version):
        return {
            "version": version,
        }

    def assert_configuration(self):
        output = self.cicdodoo("config", "--full", output=True)
        assert (
            "ODOO_QUEUEJOBS_CRON_IN_ONE_CONTAINER=1" not in output
        ), "ODOO_QUEUEJOBS_CRON_IN_ONE_CONTAINER=1 not allowed"
        assert "RUN_ODOO_QUEUEJOBS: '1'" in output, "RUN_ODOO_QUEUEJOBS=1 required"
        assert "RUN_ODOO_CRONJOBS: '1'" in output, "RUN_ODOO_CRONJOBS=1 required"

    def cicdodoo(self, *params, output=False):
        path = Path(BuiltIn().get_variable_value("${CICD_HOME}"))
        cmd = "./cicd " + " ".join(map(lambda x: f"'{x}'", params))
        return self._sshcmd(cmd, cwd=path, output=output)

    def get_sshuser(self):
        sshuser = BuiltIn().get_variable_value("${ROBOTTEST_SSH_USER}")
        return sshuser

    def get_pubkey(self):
        return rsa_file_pub.read_text()

    def get_idrsa(self):
        return rsa_file.read_text()

    def _get_hostkey(self):
        path = Path("/tmp/key")
        path.mkdir(exist_ok=True)
        shutil.copy(rsa_file, path / "id_rsa")
        shutil.copy(rsa_file_pub, path / "id_rsa.pub")
        check_call(["chmod", "500", path])
        check_call(["chmod", "400", path / "id_rsa"])
        check_call(["chmod", "400", path / "id_rsa.pub"])
        return path / "id_rsa"

    def _writefile(self, path, content):
        file = Path(tempfile.mktemp(suffix="."))
        file.write_text(content)
        rsa_file = self._get_hostkey()
        res = check_call(
            [
                "rsync",
                "-e",
                f"ssh -i {rsa_file} -o StrictHostKeyChecking=no",
                file,
                f"{self.get_sshuser()}@host.docker.internal:{path}",
            ]
        )
        file.unlink()

    def _sshcmd(self, stringcommand, output=False, cwd=None):
        if cwd:
            stringcommand = f"cd '{cwd}' || exit -1;" f"{stringcommand}"
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            self._get_hostkey(),
            f"{self.get_sshuser()}@host.docker.internal",
            f"{stringcommand}",
        ]
        if not output:
            res = check_call(cmd)
        else:
            res = check_output(cmd, encoding="utf8")
            return res

    def _prepare_git(self):
        check_call(["git", "config", "--global", "user.email", "testcicd@nowhere.com"])
        check_call(["git", "config", "--global", "user.name", "testcicd"])

    def make_odoo_repo(self, path, version):
        path = Path(path)

        if path.exists():
            shutil.rmtree(path)
        self._prepare_git()

        self._sshcmd(f"[ -e '{path}' ] && rm -Rf '{path}' || true")
        self._sshcmd(f"mkdir -p '{path}'")
        self._writefile(
            path / "MANIFEST", json.dumps(self._get_MANIFEST(version), indent=4)
        )
        self._writefile(
            path / "gimera.yml",
            yaml.dump(
                {
                    "repos": [
                        {
                            "path": "odoo",
                            "type": "integrated",
                            "url": "https://github.com/odoo/odoo",
                            "branch": version,
                        }
                    ]
                }
            ),
        )
        self._sshcmd("git init .; git add .; git commit -am 'init'", cwd=path)
        self._sshcmd("gimera apply odoo", cwd=path)
        tmppath = path.parent / f"{path.name}.tmp"
        self._sshcmd(f"rm -Rf '{tmppath}'")
        self._sshcmd(f"mv '{path}' '{tmppath}'")
        self._sshcmd(f"git clone --bare 'file://{path}.tmp' '{path}'")
        self._sshcmd(f"rm -Rf '{path}.tmp'")
