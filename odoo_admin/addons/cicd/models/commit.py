from odoo import _, api, fields, models, SUPERUSER_ID, tools
import re
from odoo.exceptions import UserError, RedirectWarning, ValidationError
from ..tools.logsio_writer import LogsIOWriter

class GitCommit(models.Model):
    _inherit = ['mail.thread', 'cicd.open.window.mixin']
    _name = 'cicd.git.commit'
    _order = 'date desc'

    short = fields.Char(compute="_compute_short")
    name = fields.Char("SHA", required=True)
    branch_ids = fields.Many2many('cicd.git.branch', string="Branches", required=True)
    date_registered = fields.Datetime("Date registered")
    date = fields.Datetime("Date")
    author = fields.Char("Author")
    author_user_ids = fields.Many2many('res.users', compute="_compute_author_users", store=False)
    author_user_id = fields.Many2one('res.users', compute="_compute_author_users")
    text = fields.Text("Text")
    test_run_ids = fields.One2many('cicd.test.run', 'commit_id', string="Test Runs")
    test_state = fields.Selection([
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], compute="_compute_test_state", tracking=True, string="Test State")
    approval_state = fields.Selection([
        ('check', "Check"),
        ('approved', 'Approved'),
        ('declined', 'Declined'),
    ], tracking=True, string="Approval")
    force_approved = fields.Boolean("Force Approved", tracking=True)

    _sql_constraints = [
        ('name', "unique(name)", _("Only one unique entry allowed.")),
    ]

    def _compute_short(self):
        for rec in self:
            rec.short = rec.name[:8]

    def _event_new_test_state(self, new_state):
        pass # implement!

    def set_to_check(self):
        self.approval_state = 'check'

    def set_approved(self):
        self.approval_state = 'approved'

    def set_declined(self):
        self.approval_state = 'declined'

    @api.recordchange('force_approved')
    def _force_approved_changed(self):
        for rec in self:
            if rec.force_approved:
                if rec.approval_state != 'approved':
                    rec.approval_state = 'approved'

    @api.depends('test_run_ids', 'test_run_ids.state')
    def _compute_test_state(self):
        for rec in self:
            testruns = rec.test_run_ids.sorted(lambda x: x.id, reverse=True)
            if not testruns or testruns[0].state in ('open', 'running'):
                rec.test_state = False
                continue
            new_state = testruns[0].state
            if new_state != rec.test_state:
                rec.test_state = new_state
                rec._event_new_test_state(new_state)

    def run_tests(self, filtered=None):
        for ttype in self.env['cicd.test.run']._get_types(filtered):
            # run tests on machine
            raise NotImplementedError("Need machine to run")

    @api.model
    def create(self, vals):
        res = super().create(vals)
        self._evaluate_message()
        return res

    def _evaluate_message(self):
        for rec in self:
            if ":REVIEW:" in rec.text:
                rec.approval_state = 'check'
            if ":TEST:" in rec.text:
                rec.branch_id.run_tests()
            if ":APPROVE:" in rec.text:
                rec.branch_id.with_user(self.author_user_id.id).approve()
            if ":RESET:" in rec.text:
                rec.branch_id.with_user(self.author_user_id.id)._make_task("_prepare_a_new_instance", silen=True)

    @tools.ormcache('self.id', 'commit')
    def contains_commit(self, commit):
        self.ensure_one()
        repo = self.mapped('branch_ids.repo_id')

        with LogsIOWriter.GET("contains_commit", "Check") as logsio:
            repo_path = repo._get_main_repo(logsio=logsio, machine=repo.machine_id)
            with repo.machine_id._shell(repo_path, logsio=logsio) as shell:
                test = shell.X(['git', 'merge-base', commit.name, self.name], allow_error=True)  # order seems to be irrelevant
                if test['exit_code']:
                    if 'fatal: Not a valid commit name' in test['stdout']:
                        return False
                return not test['exit_code']

    def view_changes(self):
        return {
            'type': 'ir.actions.act_url',
            'url': self.mapped('branch_ids.repo_id')[0].sudo()._get_url('commit', self),
            'target': 'new',
        }

    @api.depends("author")
    def _compute_author_users(self):
        for rec in self:
            if not rec.author:
                rec.author_user_ids = [[6, 0, []]]
                continue

            match = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', self.author)
            email = None
            if match:
                email = match.group(0)
            if email:
                rec.author_user_ids = self.env['res.users'].search([('login', '=', email)])
            else:
                rec.author_user_ids = [[6, 0, []]]

            if rec.author_user_ids:
                rec.author_user_id = rec.author_user_ids[0]
            else:
                rec.author_user_id = self.env.user
