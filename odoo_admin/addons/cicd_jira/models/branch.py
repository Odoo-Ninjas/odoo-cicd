from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import jira as JIRA

class Branch(models.Model):
    _inherit = 'cicd.git.branch'

    def _get_jira_issue(self):
        self.ensure_one()
        jira = self.repo_id.ticketsystem_id._get_jira_connection()
        try:
            issue_name = self.repo_id.ticketsystem_id._extract_ts_part(self)
            issue = jira.issue(issue_name)
        except JIRA.exceptions.JIRAError as jira_ex:
            if 'Issue does not exist or you do not have permission to see it' in str(jira_ex):
                return None
            raise
        return issue

    def ticketsystem_set_state(self, state):
        super().ticketsystem_set_state(state)

    def _report_new_state_to_ticketsystem(self):
        super()._report_new_state_to_ticketsystem()
        for rec in self:
            if rec.repo_id.ticketsystem_id.ttype == 'jira':
                ts = rec.repo_id.ticketsystem_id
                issue = self._get_jira_issue()
                if issue:
                    # TODO correct state
                    #ts._jira_set_state(issue, 'done')
                    pass

    def _report_comment_to_ticketsystem(self, comment):
        super()._report_comment_to_ticketsystem(comment)
        for rec in self:
            rec._jira_comment(comment)

    def _jira_comment(self, comment):
        for rec in self:
            ts = rec.repo_id.ticketsystem_id.filtered(lambda x: x.ttype == 'jira')
            if not ts:
                return
            ts._jira_comment(rec.ticket_system_ref or rec.name, comment)

    def _event_new_test_state(self, new_state):
        super()._event_new_test_state(new_state)
        comment = None
        if new_state == 'success':
            comment = "Tests Succeeded"
        elif new_state == 'failed':
            comment = "Tests failed"
        self._jira_comment(comment)