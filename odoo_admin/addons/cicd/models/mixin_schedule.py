from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT as DTF
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import arrow

class Schedule(models.AbstractModel):
    _name = 'mixin.schedule'

    # TODO: add days, and so on

    hour = fields.Integer("Hour")
    minute = fields.Integer("Minute")

    @api.model
    def _compute_next_date(self, start_from):
        test = arrow.get(
            (start_from and arrow.get(start_from) or arrow.utcnow()).strftime(
                DTF))
        test = test.replace(hour=self.hour, minute=self.minute)
        if test.strftime(DTF) < start_from.strftime(DTF):
            test = test.shift(days=1)
        return test.strftime(DTF)
