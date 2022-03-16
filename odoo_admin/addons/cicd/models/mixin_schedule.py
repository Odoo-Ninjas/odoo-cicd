from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import arrow

class Schedule(models.AbstractModel):
    _name = 'mixin.schedule'

    # TODO: add days, and so on

    hour = fields.Integer("Hour")
    minute = fields.Integer("Minute")

    def _compute_next_date(self, start_from):
        for rec in self:
            test = start_from or arrow.get(start_from) or arrow.get()
            test = test.replace(hour=self.hour, minute=self.minute)
            if test < start_from:
                test = test.shift(days=1)
            rec.next_date = test.strftime(DTF)
